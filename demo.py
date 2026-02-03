"""
Flask Webhook Server - Gmail to Jenkins Bridge

This receives webhooks from Google Apps Script and triggers Jenkins jobs.

Environment Variables Required:
- JENKINS_URL: Full Jenkins job build URL
- JENKINS_USER: Jenkins username
- JENKINS_API_TOKEN: Jenkins API token
- PORT or FLASK_PORT: (optional) Port to run on, default 5000 (Coolify uses PORT)
- FLASK_DEBUG: (optional) Enable debug mode, default False
- JENKINS_TIMEOUT: (optional) Jenkins request timeout in seconds, default 120
"""

from flask import Flask, request, jsonify
import requests
import os
import logging
import threading
import json
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
# Coolify uses PORT environment variable, but we also support FLASK_PORT for flexibility
FLASK_PORT = int(os.environ.get("PORT", os.environ.get("FLASK_PORT", "5000")))
FLASK_DEBUG = os.environ.get("FLASK_DEBUG", "False").lower() == "true"
JENKINS_TIMEOUT = int(os.environ.get("JENKINS_TIMEOUT", "120"))  # Default 120 seconds (2 minutes)

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
    """
    Health check endpoint - must always return 200 for service availability
    This is critical for Coolify and other deployment platforms to know the service is running.
    Configuration validation happens in the actual endpoints.
    """
    # Minimal logging for healthchecks to reduce log noise
    logger.debug("Health check requested")
    try:
        # Always return 200 if service is running
        # Configuration validation happens in /ready endpoint and webhook handler
        config_status = "configured" if all([JENKINS_URL, JENKINS_USER, JENKINS_API_TOKEN]) else "not_configured"
        
        return jsonify({
            "status": "healthy",
            "service": "gmail-jenkins-webhook",
            "configuration": config_status,
            "timestamp": datetime.utcnow().isoformat()
        }), 200
    except Exception as e:
        logger.error(f"Health check error: {str(e)}", exc_info=True)
        # Even on error, return 200 if service is running (let endpoints handle errors)
        return jsonify({
            "status": "healthy",
            "service": "gmail-jenkins-webhook",
            "warning": str(e),
            "timestamp": datetime.utcnow().isoformat()
        }), 200


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
    
    CRITICAL: This endpoint must return IMMEDIATELY (< 1 second) to avoid 504 timeouts.
    All processing happens asynchronously in a background thread.
    """
    # Handle GET requests immediately (for health checks or debugging)
    if request.method == "GET":
        return jsonify({
            "status": "endpoint_active",
            "message": "Gmail webhook endpoint is active. Use POST to send data.",
            "method": request.method
        }), 200
    
    # For POST: Return 202 Accepted IMMEDIATELY, process in background
    # This prevents 504 Gateway Timeout errors from proxies/load balancers
    
    # Verify service is ready (quick check)
    if not all([JENKINS_URL, JENKINS_USER, JENKINS_API_TOKEN]):
        return jsonify({
            "status": "error",
            "message": "Service not ready - missing configuration"
        }), 503
    
    # Get raw request data to pass to background thread
    # Don't parse JSON here - do it in the background thread to save time
    try:
        raw_data = request.get_data(as_text=True)
    except Exception as e:
        logger.error(f"Error reading request data: {str(e)}")
        raw_data = None
    
    # Start background processing immediately
    # All JSON parsing, validation, and Jenkins triggering happens in background
    thread = threading.Thread(
        target=process_webhook_async,
        args=(raw_data,),
        daemon=True,
        name=f"WebhookProcessor-{datetime.utcnow().isoformat()}"
    )
    thread.start()
    
    # Return 202 Accepted immediately - processing happens in background
    # This response must be sent within milliseconds to avoid proxy timeouts
    return jsonify({
        "status": "accepted",
        "message": "Webhook received and queued for processing",
        "note": "Processing happens asynchronously"
    }), 202


def process_webhook_async(raw_data):
    """
    Process webhook request asynchronously (runs in background thread)
    This does all the heavy lifting: JSON parsing, validation, Jenkins triggering
    
    Args:
        raw_data: Raw request body as string (will be parsed as JSON)
    """
    try:
        logger.info("Starting async webhook processing...")
        
        # Parse JSON data
        if not raw_data:
            logger.warning("Received empty request body in async processor")
            return
        
        try:
            data = json.loads(raw_data)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON in async processor: {str(e)}")
            return
        except Exception as e:
            logger.error(f"Error parsing request data: {str(e)}")
            return
        
        # Extract email details
        subject = data.get("subject", "")
        from_email = data.get("from", "unknown")
        message_id = data.get("messageId", "unknown")
        
        logger.info(f"Processing email - Subject: '{subject}' | From: {from_email} | ID: {message_id}")
        
        # Validate subject contains trigger keyword
        if "Practical DevSecOps" in subject:
            logger.info(f"✓ Subject matched! Triggering Jenkins job...")
            
            # Trigger Jenkins job (this is already async-safe)
            result = trigger_jenkins_job(data)
            
            if result["success"]:
                logger.info(f"✓ Jenkins job triggered successfully (async) - Status: {result.get('status_code', 'N/A')}")
            else:
                logger.error(f"✗ Jenkins trigger failed (async): {result.get('error', 'Unknown error')} - Status: {result.get('status_code', 'N/A')}")
        else:
            logger.info(f"✗ Subject did not match - ignoring email")
    
    except Exception as e:
        logger.error(f"Error in async webhook processing: {str(e)}", exc_info=True)


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
    # Define timeouts before try block so they're accessible in exception handler
    # Use a connection timeout and read timeout separately
    # Connection timeout: how long to wait to establish connection
    # Read timeout: how long to wait for response after connection
    # Increased connection timeout to 30s to handle slow networks/SSL handshakes
    connect_timeout = min(30, JENKINS_TIMEOUT // 2)  # 30s or 1/2 of total timeout
    read_timeout = JENKINS_TIMEOUT
    
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
        logger.info(f"Jenkins URL: {JENKINS_URL}")
        logger.info(f"Jenkins User: {JENKINS_USER}")
        logger.info(f"Connection timeout: {connect_timeout}s, Read timeout: {read_timeout}s")
        
        response = requests.post(
            JENKINS_URL,
            auth=(JENKINS_USER, JENKINS_API_TOKEN),
            headers=headers,
            allow_redirects=True,  # Follow redirects (Jenkins may return 302)
            # json=params,  # Uncomment to pass parameters
            timeout=(connect_timeout, read_timeout)  # (connect, read) timeout tuple
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
    
    except requests.exceptions.Timeout as e:
        # Determine if it's a connection or read timeout
        error_str = str(e)
        if "ConnectTimeout" in error_str or "connection" in error_str.lower():
            timeout_type = "connection"
            timeout_value = connect_timeout
            error_msg = f"Could not establish connection to Jenkins within {timeout_value}s"
        else:
            timeout_type = "read"
            timeout_value = read_timeout
            error_msg = f"Jenkins did not respond within {timeout_value}s after connection"
        
        logger.error(f"✗ Jenkins request timed out ({timeout_type} timeout)")
        logger.error(f"  Timeout value: {timeout_value}s")
        logger.error(f"  Jenkins URL: {JENKINS_URL}")
        logger.error(f"  Error details: {error_str}")
        
        return {
            "success": False,
            "error": error_msg,
            "timeout_type": timeout_type,
            "timeout_seconds": timeout_value,
            "message": "Check Jenkins server status, network connectivity, and firewall settings"
        }
    
    except requests.exceptions.ConnectionError as e:
        logger.error(f"✗ Could not connect to Jenkins: {str(e)}")
        logger.error(f"Jenkins URL: {JENKINS_URL}")
        return {
            "success": False,
            "error": f"Could not connect to Jenkins: {str(e)}",
            "message": "Check Jenkins URL, network connectivity, and firewall settings"
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