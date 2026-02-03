"""
Flask Webhook Server - Gmail to Jenkins Bridge

This receives webhooks from Google Apps Script and triggers Jenkins jobs.

Environment Variables Required:
- JENKINS_URL: Full Jenkins job build URL
- JENKINS_USER: Jenkins username
- JENKINS_API_TOKEN: Jenkins API token
- FLASK_PORT: (optional) Port to run on, default 5000
- FLASK_DEBUG: (optional) Enable debug mode, default False
"""

from flask import Flask, request, jsonify
import requests
import os
import logging
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

# Validate required environment variables
if not all([JENKINS_URL, JENKINS_USER, JENKINS_API_TOKEN]):
    logger.error("Missing required environment variables!")
    logger.error("Required: JENKINS_URL, JENKINS_USER, JENKINS_API_TOKEN")
    exit(1)


@app.route("/", methods=["GET"])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "service": "gmail-jenkins-webhook",
        "timestamp": datetime.utcnow().isoformat()
    })


@app.route("/gmail-webhook", methods=["POST", "GET"])
@app.route("/gmail-webhook/", methods=["POST", "GET"])
def gmail_webhook():
    """
    Main webhook endpoint
    Receives email data from Google Apps Script
    Validates subject and triggers Jenkins
    """
    try:
        # Log request details for debugging
        logger.info(f"Request method: {request.method}")
        logger.info(f"Request path: {request.path}")
        logger.info(f"Request headers: {dict(request.headers)}")
        
        # Handle GET requests (for health checks or debugging)
        if request.method == "GET":
            return jsonify({
                "status": "endpoint_active",
                "message": "Gmail webhook endpoint is active. Use POST to send data.",
                "method": request.method
            }), 200
        
        # Parse JSON data
        data = request.get_json()
        
        if not data:
            logger.warning("Received empty request body")
            return jsonify({"error": "No data received"}), 400
        
        # Extract email details
        subject = data.get("subject", "")
        from_email = data.get("from", "unknown")
        message_id = data.get("messageId", "unknown")
        
        logger.info(f"Received email - Subject: '{subject}' | From: {from_email}")
        
        # Validate subject contains trigger keyword
        if "Practical DevSecOps" in subject:
            logger.info(f"✓ Subject matched! Triggering Jenkins job...")
            
            # Trigger Jenkins job
            jenkins_response = trigger_jenkins_job(data)
            
            if jenkins_response["success"]:
                logger.info(f"✓ Jenkins job triggered successfully")
                return jsonify({
                    "status": "success",
                    "message": "Jenkins job triggered",
                    "jenkins_response": jenkins_response
                }), 200
            else:
                logger.error(f"✗ Jenkins trigger failed: {jenkins_response['error']}")
                return jsonify({
                    "status": "error",
                    "message": "Failed to trigger Jenkins",
                    "details": jenkins_response
                }), 500
        
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


def trigger_jenkins_job(email_data):
    """
    Trigger Jenkins job via REST API
    
    Args:
        email_data: Dictionary with email details
        
    Returns:
        Dictionary with success status and details
    """
    try:
        # Prepare Jenkins request
        headers = {
            "Content-Type": "application/json"
        }
        
        # Optional: Pass email data as parameters to Jenkins
        # params = {
        #     "EMAIL_SUBJECT": email_data.get("subject", ""),
        #     "EMAIL_FROM": email_data.get("from", "")
        # }
        
        # Make request to Jenkins
        response = requests.post(
            JENKINS_URL,
            auth=(JENKINS_USER, JENKINS_API_TOKEN),
            headers=headers,
            # json=params,  # Uncomment to pass parameters
            timeout=10
        )
        
        # Check response
        if response.status_code in [200, 201]:
            return {
                "success": True,
                "status_code": response.status_code,
                "message": "Jenkins job triggered successfully"
            }
        else:
            return {
                "success": False,
                "status_code": response.status_code,
                "error": response.text,
                "message": "Jenkins returned non-success status"
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
    logger.info(f"Incoming request: {request.method} {request.path}")
    logger.info(f"Headers: {dict(request.headers)}")
    if request.is_json:
        logger.info(f"JSON payload: {request.get_json()}")


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


if __name__ == "__main__":
    logger.info("=" * 50)
    logger.info("Gmail-Jenkins Webhook Server Starting")
    logger.info(f"Jenkins URL: {JENKINS_URL}")
    logger.info(f"Jenkins User: {JENKINS_USER}")
    logger.info(f"Listening on port: {FLASK_PORT}")
    logger.info("=" * 50)
    
    app.run(
        host="0.0.0.0",
        port=FLASK_PORT,
        debug=FLASK_DEBUG
    )