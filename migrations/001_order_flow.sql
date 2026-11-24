-- Run once with the Supabase SQL editor before deploying the new bot.
begin;
create table if not exists public.orders (
    id uuid primary key default gen_random_uuid(), telegram_user_id bigint not null,
    telegram_chat_id bigint not null, customer_phone text not null, image_url text not null,
    items jsonb not null, subtotal numeric not null, discount_percent integer not null,
    total numeric not null, deposit_amount numeric not null, delivery_method text not null,
    delivery_address text, status text not null default 'pending_deposit',
    created_at timestamptz default now(), updated_at timestamptz default now()
);
create table public.artwork_uploads (
    id uuid primary key default gen_random_uuid(), owner_id bigint not null,
    ordered boolean not null default false, message_id bigint not null, extension text not null, deleting boolean not null default false,
    expires_at timestamptz not null default now() + interval '24 hours',
    unique (owner_id, message_id)
);
alter table public.orders
    add column upload_id uuid references public.artwork_uploads(id) unique,
    add column deposit_paid boolean,
    add column balance_received_at timestamptz,
    add column balance_received_by bigint,
    add column balance_method text check (balance_method in ('cash', 'transfer', 'other'));
-- Staff verify legacy deposits against their records before further fulfillment.
update public.orders set deposit_paid = false where status = 'pending_deposit';
alter table public.orders alter column deposit_paid set default false;
create index orders_customer_created on public.orders(telegram_user_id, created_at desc);

create function public.validate_order_upload() returns trigger language plpgsql set search_path = public as $$
begin
    if new.upload_id is not null then
        update artwork_uploads set ordered = true where id = new.upload_id and owner_id = new.telegram_user_id
            and not deleting and expires_at > now();
        if not found then raise exception 'Artwork unavailable'; end if;
    end if;
    return new;
end $$;
create trigger validate_order_upload before insert on public.orders
    for each row execute function public.validate_order_upload();

create function public.claim_expired_artwork() returns setof public.artwork_uploads
language sql set search_path = public as $$
    update artwork_uploads set deleting = true where id in (
        select u.id from artwork_uploads u where not u.ordered and u.expires_at <= now()
        and not exists (select 1 from orders o where o.upload_id = u.id)
        order by expires_at limit 100 for update of u skip locked
    ) and not ordered
    returning *;
$$;

create table public.order_notifications (
    id bigint generated always as identity primary key, order_id uuid not null references public.orders(id),
    chat_id bigint not null, event text not null, available_at timestamptz not null default now(),
    attempts integer not null default 0, sent_at timestamptz, failed boolean not null default false,
    unique(order_id,event)
);
create function public.queue_order_notification() returns trigger language plpgsql set search_path = public as $$
declare event_name text;
begin
    if new.deposit_paid and old.deposit_paid is distinct from true then event_name := 'deposit_confirmed';
    elsif new.status <> old.status then
        event_name := case when new.status = 'out_for_delivery' and new.delivery_method = 'pickup' then 'ready_for_pickup'
                           when new.status = 'completed' and new.delivery_method = 'pickup' then 'collected'
                           when new.status = 'completed' then 'delivered' else new.status end;
    end if;
    if event_name is not null then
        insert into order_notifications(order_id,chat_id,event) values(new.id,new.telegram_user_id,event_name)
        on conflict do nothing;
    end if;
    return new;
end $$;
create trigger queue_order_notification after update of status, deposit_paid on public.orders
    for each row execute function public.queue_order_notification();

create function public.claim_notification(p_order uuid default null) returns setof public.order_notifications
language sql set search_path = public as $$
    update order_notifications set attempts = attempts + 1, available_at = now() + interval '5 minutes'
    where id = (select n.id from order_notifications n where (p_order is null or n.order_id = p_order)
        and n.sent_at is null and not n.failed and n.available_at <= now()
        and not exists (select 1 from order_notifications earlier where earlier.order_id = n.order_id
            and earlier.id < n.id and earlier.sent_at is null and not earlier.failed)
        order by n.id limit 1 for update of n skip locked)
    returning *;
$$;

alter table public.orders enable row level security;
alter table public.artwork_uploads enable row level security;
alter table public.order_notifications enable row level security;
revoke all on public.orders, public.artwork_uploads, public.order_notifications from public, anon, authenticated;
grant select,insert,update,delete on public.orders, public.artwork_uploads, public.order_notifications to service_role;
revoke execute on function public.claim_notification(uuid),
    public.claim_expired_artwork() from public, anon, authenticated;
grant execute on function public.claim_notification(uuid),
    public.claim_expired_artwork() to service_role;
create table public.telegram_updates (
    id bigint primary key, lease uuid not null, expires_at timestamptz not null, done boolean not null default false
);
alter table public.telegram_updates enable row level security;
revoke all on public.telegram_updates from public, anon, authenticated;
grant select, insert, update, delete on public.telegram_updates to service_role;
create function public.claim_telegram_update(p_id bigint, p_lease uuid) returns text
language plpgsql set search_path = public as $$
begin
    insert into telegram_updates(id,lease,expires_at) values(p_id,p_lease,now()+interval '5 minutes')
    on conflict(id) do update set lease=excluded.lease, expires_at=excluded.expires_at
        where not telegram_updates.done and telegram_updates.expires_at < now();
    if found then return 'claimed'; end if;
    if exists(select 1 from telegram_updates where id=p_id and done) then return 'done'; end if;
    return 'busy';
end $$;
revoke execute on function public.claim_telegram_update(bigint,uuid) from public, anon, authenticated;
grant execute on function public.claim_telegram_update(bigint,uuid) to service_role;
insert into storage.buckets(id,name,public) values('sticker-artwork-private','sticker-artwork-private',false)
    on conflict (id) do nothing;
commit;
