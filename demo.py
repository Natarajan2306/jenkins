from flask import Flask, jsonify
import imaplib
import email
import requests
import threading
import time
import urllib.parse

app = Flask(__name__)

# ========== YOUR SETTINGS (ALL CONFIGURED) ==========
GMAIL_EMAIL = 'natarajan@pdevsecops.com'
GMAIL_APP_PASSWORD = 'gkqlnyojggevgnhd'  # Spaces removed
JENKINS_URL = 'http://localhost:8080'
JENKINS_USER = 'admin'
JENKINS_TOKEN = '1154319e687396663934958c01737c99b9'
JOB_NAME = 'pre-sign up automations'

# Trigger words in subject
TRIGGER_SUBJECTS = ['START JENKINS', 'DEPLOY NOW', 'RUN BUILD', 'TRIGGER', 'PRE-SIGN UP']
# ====================================================

monitoring = True

def trigger_jenkins():
    """Trigger Jenkins job"""
    # URL encode job name (handles spaces)
    encoded_job = urllib.parse.quote(JOB_NAME, safe='')
    url = f"{JENKINS_URL}/job/{encoded_job}/build"
    
    try:
        print(f"   🚀 Calling: {url}")
        response = requests.post(url, auth=(JENKINS_USER, JENKINS_TOKEN), timeout=10)
        
        if response.status_code == 201:
            print("   ✅ Jenkins job 'pre-sign up automations' triggered successfully!")
            return True
        elif response.status_code == 404:
            print(f"   ❌ Job not found! Check job name in Jenkins")
            print(f"   💡 Make sure job exists: {JENKINS_URL}/job/{encoded_job}")
            return False
        elif response.status_code == 403:
            print(f"   ❌ Permission denied! Check your token")
            return False
        else:
            print(f"   ❌ Jenkins failed: Status {response.status_code}")
            print(f"   Response: {response.text}")
            return False
    except Exception as e:
        print(f"   ❌ Error: {e}")
        return False

def check_latest_email(mail):
    """Check the most recent email"""
    try:
        # Get the latest email
        status, messages = mail.search(None, 'ALL')
        email_ids = messages[0].split()
        
        if not email_ids:
            return
        
        latest_id = email_ids[-1]
        
        # Fetch it
        status, msg_data = mail.fetch(latest_id, '(RFC822)')
        
        for response_part in msg_data:
            if isinstance(response_part, tuple):
                msg = email.message_from_bytes(response_part[1])
                subject = msg['subject'] or ""
                from_email = msg['from']
                date = msg['date']
                
                print(f"\n⚡ NEW EMAIL DETECTED!")
                print(f"   📨 Subject: '{subject}'")
                print(f"   👤 From: {from_email}")
                print(f"   📅 Date: {date}")
                
                # Check if subject matches
                subject_upper = subject.upper()
                matched = False
                for trigger in TRIGGER_SUBJECTS:
                    if trigger in subject_upper:
                        print(f"   ✅ MATCH! Found trigger word: '{trigger}'")
                        matched = True
                        trigger_jenkins()
                        break
                
                if not matched:
                    print(f"   ⏭️  No trigger word found. Skipping.")
                    print(f"   💡 Trigger words: {TRIGGER_SUBJECTS}")
                
    except Exception as e:
        print(f"❌ Error checking email: {e}")

def idle_monitor():
    """Monitor Gmail using IDLE - waits for new emails"""
    global monitoring
    
    print("🔌 Connecting to Gmail IMAP...")
    
    while monitoring:
        try:
            # Connect
            mail = imaplib.IMAP4_SSL('imap.gmail.com')
            mail.login(GMAIL_EMAIL, GMAIL_APP_PASSWORD)
            mail.select('inbox')
            
            print("✅ Connected to Gmail IMAP successfully")
            print("👀 IDLE mode activated - waiting for emails...")
            print("   💡 The script will wake up INSTANTLY when email arrives!\n")
            
            while monitoring:
                # Start IDLE mode
                tag = b'A001'
                mail.send(b'%s IDLE\r\n' % tag)
                
                # Wait for response (blocks here until email arrives)
                while True:
                    line = mail.readline()
                    
                    if b'EXISTS' in line:
                        # NEW EMAIL ARRIVED!
                        print("\n🔔 DING! Email notification received!")
                        
                        # Exit IDLE
                        mail.send(b'DONE\r\n')
                        # Clear buffer
                        while True:
                            resp = mail.readline()
                            if b'OK' in resp or b'IDLE' in resp:
                                break
                        
                        # Check the email
                        check_latest_email(mail)
                        
                        print("\n👀 Back to IDLE mode - waiting for next email...\n")
                        break
                    
                    # Timeout after 29 minutes (Gmail IDLE limit)
                    if b'OK' in line:
                        # IDLE timed out, restart it
                        break
            
        except Exception as e:
            print(f"\n❌ Connection error: {e}")
            print("🔄 Reconnecting in 10 seconds...")
            time.sleep(10)

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'running' if monitoring else 'stopped',
        'gmail': GMAIL_EMAIL,
        'jenkins': JENKINS_URL,
        'job': JOB_NAME,
        'triggers': TRIGGER_SUBJECTS
    }), 200

@app.route('/test-jenkins', methods=['POST'])
def test_jenkins():
    """Manually test Jenkins trigger"""
    print("\n🧪 Manual Jenkins test triggered...")
    if trigger_jenkins():
        return jsonify({'status': 'success', 'message': 'Jenkins triggered'}), 200
    else:
        return jsonify({'status': 'failed', 'message': 'Jenkins trigger failed'}), 500

@app.route('/stop', methods=['POST'])
def stop():
    global monitoring
    monitoring = False
    return jsonify({'status': 'stopping'}), 200

if __name__ == '__main__':
    print("\n" + "="*70)
    print("⚡ INSTANT Gmail-to-Jenkins Trigger (IMAP IDLE)")
    print("="*70)
    print(f"📧 Gmail: {GMAIL_EMAIL}")
    print(f"🎯 Jenkins: {JENKINS_URL}")
    print(f"👤 User: {JENKINS_USER}")
    print(f"📋 Job: '{JOB_NAME}'")
    print(f"🔑 Triggers: {TRIGGER_SUBJECTS}")
    print("="*70)
    print("\n💡 HOW TO TEST:")
    print("   1. Send email to natarajan@pdevsecops.com")
    print("   2. Subject must contain: TRIGGER (or any trigger word)")
    print("   3. Watch this terminal - it will trigger INSTANTLY!")
    print("\n🌐 API Endpoints:")
    print("   Health: http://localhost:5000/health")
    print("   Test Jenkins: POST to http://localhost:5000/test-jenkins")
    print("\n")
    
    # Start IDLE monitor in background
    monitor_thread = threading.Thread(target=idle_monitor, daemon=True)
    monitor_thread.start()
    
    # Start Flask
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)