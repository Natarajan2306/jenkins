"""
Flask Webhook Server - Gmail to Jenkins Bridge

This receives webhooks from Google Apps Script and triggers Jenkins jobs.

Environment Variables Required:
- JENKINS_URL: Full Jenkins job build URL
- JENKINS_USER: Jenkins username
- JENKINS_API_TOKEN: Jenkins API token
- FLASK_PORT: (optional) Port to run on, default 5000
- FLASK_DEBUG: (optional) Enable debug mode, default False
- JENKINS_TIMEOUT: (optional) Jenkins request timeout in seconds, default 60
"""

from flask import Flask, request, jsonify
import requests
import os
import logging
import threading
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)

# Configure for reverse proxy (Coolify, nginx, etc.)
# This ensures Flask correctly handles requests behind a proxy
app.config['PREFERRED_URL_SCHEME'] = 'https'

# Load configuration from environment variables
JENKINS_URL = os.environ.get("JENKINS_URL")
JENKINS_USER = os.environ.get("JENKINS_USER")
JENKINS_API_TOKEN = os.environ.get("JENKINS_API_TOKEN")
FLASK_PORT = int(os.environ.get("FLASK_PORT", "5000"))
FLASK_DEBUG = os.environ.get("FLASK_DEBUG", "False").lower() == "true"
JENKINS_TIMEOUT = int(os.environ.get("JENKINS_TIMEOUT", "60"))  # Default 60 seconds

# Validate required environment variables
# Note: We don't exit here if running under gunicorn, as it will cause the worker to crash
# Instead, we check in the health endpoint and webhook handler
if not all([JENKINS_URL, JENKINS_USER, JENKINS_API_TOKEN]):
    logger.error("Missing required environment variables!")
    logger.error("Required: JENKINS_URL, JENKINS_USER, JENKINS_API_TOKEN")
    logger.warning("Service will start but endpoints will return 503 until configured")
    # Don't exit - let the service start and return 503 from endpoints
    # This allows health checks to work and helps with debugging


@app.route("/", methods=["GET"])
def health_check():
    """Health check endpoint - must always return 200 for service availability"""
    # Minimal logging for healthchecks to reduce log noise
    logger.debug("Health check requested")
    try:
        # Verify critical environment variables are set
        if not all([JENKINS_URL, JENKINS_USER, JENKINS_API_TOKEN]):
            logger.error("Health check failed: Missing required environment variables")
            return jsonify({
                "status": "unhealthy",
                "service": "gmail-jenkins-webhook",
                "error": "Missing required environment variables",
                "timestamp": datetime.utcnow().isoformat()
            }), 503
        
        return jsonify({
            "status": "healthy",
            "service": "gmail-jenkins-webhook",
            "timestamp": datetime.utcnow().isoformat()
        }), 200
    except Exception as e:
        logger.error(f"Health check error: {str(e)}", exc_info=True)
        return jsonify({
            "status": "unhealthy",
            "service": "gmail-jenkins-webhook",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }), 503


@app.route("/ping", methods=["GET", "POST"])
def ping():
    """Quick ping endpoint to test connectivity"""
    return jsonify({
        "status": "pong",
        "timestamp": datetime.utcnow().isoformat()
    }), 200


@app.route("/ready", methods=["GET"])
def readiness_check():
    """Readiness check endpoint - verifies service is ready to accept requests"""
    try:
        # Check all required configuration
        missing = []
        if not JENKINS_URL:
            missing.append("JENKINS_URL")
        if not JENKINS_USER:
            missing.append("JENKINS_USER")
        if not JENKINS_API_TOKEN:
            missing.append("JENKINS_API_TOKEN")
        
        if missing:
            logger.warning(f"Readiness check failed: Missing {', '.join(missing)}")
            return jsonify({
                "status": "not_ready",
                "missing": missing,
                "timestamp": datetime.utcnow().isoformat()
            }), 503
        
        return jsonify({
            "status": "ready",
            "timestamp": datetime.utcnow().isoformat()
        }), 200
    except Exception as e:
        logger.error(f"Readiness check error: {str(e)}", exc_info=True)
        return jsonify({
            "status": "not_ready",
            "error": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }), 503


@app.route("/gmail-webhook", methods=["POST", "GET"])
@app.route("/gmail-webhook/", methods=["POST", "GET"])
def gmail_webhook():
    """
    Main webhook endpoint
    Receives email data from Google Apps Script
    Validates subject and triggers Jenkins
    """
    # Log immediately to confirm request reached handler
    logger.info(f"✓ Webhook handler called: {request.method} {request.path}")
    
    # Verify service is ready
    if not all([JENKINS_URL, JENKINS_USER, JENKINS_API_TOKEN]):
        logger.error("Service not ready: Missing required environment variables")
        return jsonify({
            "status": "error",
            "message": "Service not ready - missing configuration"
        }), 503
    
    try:
        # Handle GET requests first (for health checks or debugging) - return immediately
        if request.method == "GET":
            return jsonify({
                "status": "endpoint_active",
                "message": "Gmail webhook endpoint is active. Use POST to send data.",
                "method": request.method
            }), 200
        
        # Parse JSON data first (before heavy logging)
        data = request.get_json()
        
        if not data:
            logger.warning("Received empty request body")
            return jsonify({"error": "No data received"}), 400
        
        # Extract email details
        subject = data.get("subject", "")
        from_email = data.get("from", "unknown")
        message_id = data.get("messageId", "unknown")
        
        logger.info(f"Received email - Subject: '{subject}' | From: {from_email} | ID: {message_id}")
        
        # Validate subject contains trigger keyword
        if "Practical DevSecOps" in subject:
            logger.info(f"✓ Subject matched! Triggering Jenkins job...")
            
            # Trigger Jenkins job asynchronously to avoid blocking the worker
            # This prevents gunicorn worker timeouts if Jenkins is slow
            thread = threading.Thread(
                target=trigger_jenkins_job_async,
                args=(data,),
                daemon=True,
                name=f"JenkinsTrigger-{message_id}"
            )
            thread.start()
            logger.info(f"Async thread started for Jenkins trigger (thread: {thread.name})")
            
            # Return immediately - Jenkins trigger happens in background
            # This must return quickly to avoid proxy timeouts
            response = jsonify({
                "status": "accepted",
                "message": "Jenkins job trigger initiated",
                "note": "Job is being triggered asynchronously"
            })
            return response, 202
        
        else:
            logger.info(f"✗ Subject did not match - ignoring email")
            return jsonify({
                "status": "ignored",
                "message": "Email subject did not match trigger keyword"
            }), 200
    
    except Exception as e:
        logger.error(f"Error processing webhook: {str(e)}", exc_info=True)
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


def trigger_jenkins_job_async(email_data):
    """
    Trigger Jenkins job asynchronously (runs in background thread)
    Logs results but doesn't return anything
    
    Args:
        email_data: Dictionary with email details
    """
    try:
        logger.info(f"Starting async Jenkins trigger for email: {email_data.get('messageId', 'unknown')}")
        result = trigger_jenkins_job(email_data)
        if result["success"]:
            logger.info(f"✓ Jenkins job triggered successfully (async) - Status: {result.get('status_code', 'N/A')}")
        else:
            logger.error(f"✗ Jenkins trigger failed (async): {result.get('error', 'Unknown error')} - Status: {result.get('status_code', 'N/A')}")
    except Exception as e:
        logger.error(f"✗ Exception in async Jenkins trigger: {str(e)}", exc_info=True)


def trigger_jenkins_job(email_data):
    """
    Trigger Jenkins job via REST API
    
    Args:
        email_data: Dictionary with email details
        
    Returns:
        Dictionary with success status and details
    """
    try:
        logger.info(f"Preparing Jenkins request to: {JENKINS_URL}")
        # Prepare Jenkins request
        # Note: Jenkins build trigger typically doesn't need Content-Type header
        # Some Jenkins instances may reject requests with Content-Type: application/json
        headers = {}
        
        # Optional: Pass email data as parameters to Jenkins
        # params = {
        #     "EMAIL_SUBJECT": email_data.get("subject", ""),
        #     "EMAIL_FROM": email_data.get("from", "")
        # }
        
        # Make request to Jenkins
        # allow_redirects=True to follow 302 redirects that Jenkins may return
        logger.info(f"Sending POST request to Jenkins (timeout: {JENKINS_TIMEOUT}s)...")
        response = requests.post(
            JENKINS_URL,
            auth=(JENKINS_USER, JENKINS_API_TOKEN),
            headers=headers,
            allow_redirects=True,  # Follow redirects (Jenkins may return 302)
            # json=params,  # Uncomment to pass parameters
            timeout=JENKINS_TIMEOUT
        )
        logger.info(f"Jenkins responded with status code: {response.status_code}")
        logger.info(f"Jenkins response headers: {dict(response.headers)}")
        
        # Jenkins can return various status codes for successful triggers:
        # - 200: OK (some Jenkins versions)
        # - 201: Created (standard success)
        # - 302: Redirect (also indicates success, redirects to queue/item page)
        # - 303: See Other (also indicates success)
        if response.status_code in [200, 201, 302, 303]:
            location = response.headers.get('Location', 'N/A')
            logger.info(f"✓ Jenkins job triggered successfully - Location: {location}")
            return {
                "success": True,
                "status_code": response.status_code,
                "location": location,
                "message": "Jenkins job triggered successfully"
            }
        else:
            error_text = response.text[:500] if response.text else "No response body"
            logger.error(f"✗ Jenkins returned non-success status {response.status_code}: {error_text}")
            return {
                "success": False,
                "status_code": response.status_code,
                "error": error_text,
                "message": f"Jenkins returned status {response.status_code}"
            }
    
    except requests.exceptions.Timeout:
        return {
            "success": False,
            "error": "Request to Jenkins timed out",
            "message": "Jenkins may be slow or unreachable"
        }
    
    except requests.exceptions.ConnectionError:
        return {
            "success": False,
            "error": "Could not connect to Jenkins",
            "message": "Check Jenkins URL and network connectivity"
        }
    
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "message": "Unexpected error triggering Jenkins"
        }


@app.route("/test", methods=["POST"])
def test_endpoint():
    """
    Test endpoint - manually trigger Jenkins without email validation
    Usage: curl -X POST http://server:5000/test
    """
    logger.info("Test endpoint called - triggering Jenkins directly")
    
    test_data = {
        "subject": "Test trigger",
        "from": "test@example.com",
        "messageId": "test-123"
    }
    
    result = trigger_jenkins_job(test_data)
    
    return jsonify({
        "status": "test",
        "jenkins_result": result
    })


@app.before_request
def log_request_info():
    """Log all incoming requests for debugging"""
    # Skip verbose logging for healthcheck requests
    if request.path == "/" and request.method == "GET":
        logger.debug(f"Healthcheck: {request.method} {request.path}")
        return
    
    # For webhook endpoints, log minimal info to avoid blocking
    # Full logging will happen in the handler after we start processing
    if request.path.startswith("/gmail-webhook"):
        logger.info(f"Webhook request: {request.method} {request.path}")
        return  # Don't do heavy logging here - return immediately
    
    # For other endpoints, do full logging
    logger.info(f"Incoming request: {request.method} {request.path}")
    logger.info(f"Request URL: {request.url}")
    logger.info(f"Request base URL: {request.base_url}")
    logger.info(f"Headers: {dict(request.headers)}")
    if request.is_json:
        try:
            logger.info(f"JSON payload: {request.get_json()}")
        except:
            logger.warning("Could not parse JSON payload")


@app.errorhandler(404)
def not_found(e):
    """Handle 404 errors with detailed logging"""
    logger.error(f"404 Not Found: {request.method} {request.path}")
    logger.error(f"Request URL: {request.url}")
    logger.error(f"Request base URL: {request.base_url}")
    logger.error(f"Request script root: {request.script_root}")
    logger.error(f"Request path: {request.path}")
    logger.error(f"Request URL root: {request.url_root}")
    
    # List all registered routes for debugging
    routes = []
    for rule in app.url_map.iter_rules():
        routes.append({
            "endpoint": rule.endpoint,
            "methods": list(rule.methods),
            "path": str(rule)
        })
    logger.error(f"Available routes: {routes}")
    
    return jsonify({
        "error": "Not found",
        "method": request.method,
        "path": request.path,
        "url": request.url,
        "available_routes": routes,
        "message": f"Route {request.path} not found"
    }), 404


@app.errorhandler(405)
def method_not_allowed(e):
    """Handle 405 errors with detailed logging"""
    logger.error(f"405 Method Not Allowed: {request.method} {request.path}")
    logger.error(f"Allowed methods for {request.path}: {e.valid_methods if hasattr(e, 'valid_methods') else 'unknown'}")
    return jsonify({
        "error": "Method not allowed",
        "method": request.method,
        "path": request.path,
        "message": f"{request.method} is not allowed for {request.path}"
    }), 405


@app.route("/routes", methods=["GET"])
def list_routes():
    """List all registered routes for debugging"""
    routes = []
    for rule in app.url_map.iter_rules():
        routes.append({
            "endpoint": rule.endpoint,
            "methods": list(rule.methods),
            "path": str(rule)
        })
    return jsonify({
        "routes": routes,
        "total": len(routes)
    })


# Log registered routes on startup (works with gunicorn)
# This will be called after all routes are registered
def log_startup_info():
    """Log startup information including registered routes"""
    logger.info("=" * 50)
    logger.info("Flask App Initialized")
    logger.info(f"Jenkins URL: {JENKINS_URL}")
    logger.info(f"Jenkins User: {JENKINS_USER}")
    logger.info("Registered routes:")
    for rule in app.url_map.iter_rules():
        logger.info(f"  {list(rule.methods)} {rule}")
    logger.info("=" * 50)


# Log startup info when module is imported (for gunicorn)
# This runs once when the module is loaded
# Wrap in try-except to prevent import failures
def initialize_service():
    """Initialize and validate service configuration"""
    try:
        log_startup_info()
        
        # Validate configuration
        missing = []
        if not JENKINS_URL:
            missing.append("JENKINS_URL")
        if not JENKINS_USER:
            missing.append("JENKINS_USER")
        if not JENKINS_API_TOKEN:
            missing.append("JENKINS_API_TOKEN")
        
        if missing:
            logger.warning(f"⚠️  Service starting with missing configuration: {', '.join(missing)}")
            logger.warning("Endpoints will return 503 until configuration is complete")
        else:
            logger.info("✓ Service initialized successfully with all configuration")
            
    except Exception as e:
        logger.error(f"Error during startup: {e}", exc_info=True)
        # Don't raise - let the service start even if logging fails
        logger.warning("Service will continue but may not function correctly")

# Initialize on import (for gunicorn)
# This ensures the app is ready when gunicorn loads it
try:
    initialize_service()
except Exception as e:
    # Critical: Don't let initialization errors prevent the service from starting
    logger.error(f"Critical error during initialization: {e}", exc_info=True)
    logger.error("Service will attempt to start anyway - check configuration")

# Only run Flask dev server if executed directly (not via gunicorn)
if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("Gmail-Jenkins Webhook Server Starting (Development Mode)")
    logger.info(f"Jenkins URL: {JENKINS_URL}")
    logger.info(f"Jenkins User: {JENKINS_USER}")
    logger.info(f"Listening on port: {FLASK_PORT}")
    
    app.run(
        host="0.0.0.0",
        port=FLASK_PORT,
        debug=FLASK_DEBUG
    )