"""PythonAnywhere WSGI entry point.

PythonAnywhere expects a module-level variable named `application` that is a
WSGI callable. This file simply imports the Flask app and re-exports it under
the correct name.

Do NOT add app.run() here -- PythonAnywhere's web server handles request
serving. Adding app.run() would conflict with the WSGI container.

PythonAnywhere WSGI configuration file should point to this file, and the
import line in the auto-generated config should be:

    from wsgi import application
"""

from app import app as application  # noqa: F401
