#!/usr/bin/env python3
import subprocess, yaml, os, json, urllib.request, time, io, threading
from datetime import datetime, timedelta
import psutil
import re
import functools
from pathlib import Path
from flask import Flask, render_template_string, request, redirect, url_for, send_file, jsonify, session, send_from_directory
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename

# Import theme manager
try:
    import theme_manager
except ImportError:
    theme_manager = None
    print("Warning: theme_manager not found, theme features will be disabled")

# Load environment variables
try:
    from dotenv import load_dotenv
    # Try to load from /opt/pi-config/.env first, then fallback to local
    env_path = Path('/opt/pi-config/.env')
    if env_path.exists():
        load_dotenv(env_path)
    else:
        load_dotenv()  # Load from current directory
except ImportError:
    print("Warning: python-dotenv not installed, using default values")

# Configuration from environment variables
DASHBOARD_PASSWORD = os.getenv('DASHBOARD_PASSWORD', 'admin')
PIHOLE_PASSWORD = os.getenv('PIHOLE_PASSWORD', 'admin')
WEB_PORT = int(os.getenv('WEB_PORT', '8080'))

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'openpirouter-secret-key-2024')
socketio = SocketIO(app, cors_allowed_origins="*")

# Caching system
cache = {}
cache_timeout = 5  # seconds

def cached_function(func):
    """Decorator for caching function results"""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        cache_key = f"{func.__name__}_{hash(str(args) + str(kwargs))}"
        now = time.time()
        
        # Check if cache is valid
        if cache_key in cache:
            cached_data, timestamp = cache[cache_key]
            if now - timestamp < cache_timeout:
                return cached_data
        
        # Execute function and cache result
        result = func(*args, **kwargs)
        cache[cache_key] = (result, now)
        return result
    
    return wrapper

# WebSocket event handlers
@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    print(f"Client connected: {request.sid}")
    emit('status', {'message': 'Connected to OpenPiRouter Dashboard'})

@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    print(f"Client disconnected: {request.sid}")

def background_update_task():
    """Background task to send real-time updates"""
    print("🔄 Background update thread started")
    update_count = 0
    
    while True:
        try:
            update_count += 1
            print(f"📡 Sending update #{update_count}")
            
            # Get fresh data (bypass cache for real-time updates)
            status_data = get_system_status.__wrapped__()
            stats_data = get_system_stats.__wrapped__()
            
            # Get WiFi info
            wifi_data = get_current_wifi_data()
            speed_data = get_internet_speed_data()
            
            # Get connected clients
            ap_info = get_ap_info()
            
            # Send updates to all connected clients
            socketio.emit('system_status', status_data)
            socketio.emit('system_stats', stats_data)
            socketio.emit('wifi_status', wifi_data)
            socketio.emit('speed_data', speed_data)
            socketio.emit('client_list', {'clients': ap_info.get('clients', []),'count': len(ap_info.get('clients', []))})
            
            print(f"✅ Update #{update_count} sent successfully")
            
        except Exception as e:
            print(f"❌ Error in background update #{update_count}: {e}")
        
        time.sleep(3)  # Update every 3 seconds

def get_current_wifi_data():
    """Get current WiFi connection data with smart timeout handling"""
    try:
        # First check if we have an active connection (fast check)
        result = subprocess.run(["nmcli", "-t", "-f", "name,device,state", "con", "show", "--active"], 
                              capture_output=True, text=True, timeout=5)
        
        if result.returncode != 0:
            return {'connected': False}
        
        wifi_connected = False
        connection_name = ""
        
        for line in result.stdout.splitlines():
            if "wlan0" in line:
                parts = line.split(":")
                if len(parts) >= 3:
                    name = parts[0]
                    device = parts[1]
                    state = parts[2]
                    if device == "wlan0" and ("activated" in state or "connected" in state):
                        wifi_connected = True
                        connection_name = name
                        break
        
        if not wifi_connected:
            return {'connected': False}
        
        # If connected, try to get signal strength with smart timeout
        # Use a shorter timeout and fallback to previous signal if it fails
        try:
            signal_result = subprocess.run(["nmcli", "-t", "-f", "ssid,signal", "dev", "wifi", "list", "ifname", "wlan0"], 
                                         capture_output=True, text=True, timeout=3)
            signal = "0"
            if signal_result.returncode == 0:
                for signal_line in signal_result.stdout.splitlines():
                    if connection_name in signal_line:
                        parts = signal_line.split(":")
                        if len(parts) >= 2:
                            signal = parts[1].strip()
                            break
        except subprocess.TimeoutExpired:
            # If signal scan times out, but we know we're connected, use last known signal
            signal = "0"  # Fallback to 0 if we can't get signal
        
        return {
            'connected': True,
            'ssid': connection_name,
            'signal': signal
        }
        
    except Exception as e:
        return {'connected': False, 'error': str(e)}

def get_internet_speed_data():
    """Get internet speed data - WAN input and AP output"""
    try:
        result = subprocess.run(["cat", "/proc/net/dev"], capture_output=True, text=True, timeout=5)
        
        if result.returncode != 0:
            return {'success': False, 'error': result.stderr}
        
        lines = result.stdout.splitlines()
        
        # WAN interfaces (Internet-Eingang)
        wan_rx = 0  # Download from Internet
        wan_tx = 0  # Upload to Internet
        
        # AP interfaces (zu Clients)
        ap_rx = 0   # von Clients empfangen
        ap_tx = 0   # zu Clients gesendet
        
        for line in lines:
            parts = line.split()
            if len(parts) >= 10:
                try:
                    interface = parts[0].rstrip(':')
                    rx_bytes = int(parts[1])
                    tx_bytes = int(parts[9])
                    
                    # WAN interfaces: wlan0 (WiFi WAN) and eth0 (Ethernet WAN)
                    if interface in ['wlan0', 'eth0']:
                        wan_rx += rx_bytes
                        wan_tx += tx_bytes
                    
                    # AP interfaces: wlan1 (AP) and br0 (bridge with eth0)
                    elif interface in ['wlan1', 'br0']:
                        ap_rx += rx_bytes
                        ap_tx += tx_bytes
                        
                except (ValueError, IndexError):
                    continue
        
        return {
            'success': True,
            'wan_rx': wan_rx,      # Internet download (↓)
            'ap_tx': ap_tx,        # zu Clients (↑)
            'timestamp': int(time.time() * 1000)
        }
        
    except Exception as e:
        return {'success': False, 'error': str(e)}

CONF_FILE = "/etc/pi-repeater.yaml"
HOSTAPD = "/etc/hostapd/hostapd.conf"
LEASES = "/var/lib/misc/dnsmasq.leases"

# Modern Dashboard Template
DASHBOARD_TEMPLATE = """<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OpenPiRouter Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            color: #333;
        }
        .container { max-width: 1400px; margin: 0 auto; padding: 20px; }
        .header { 
            background: rgba(255,255,255,0.95);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            padding: 20px;
            margin-bottom: 20px;
            box-shadow: 0 8px 32px rgba(0,0,0,0.1);
        }
        .header h1 { 
            color: #2d3748;
            font-size: 2.5em;
            font-weight: 700;
            margin-bottom: 10px;
        }
        
        .logout-btn {
            background: #e53e3e;
            color: white;
            padding: 8px 16px;
            border-radius: 8px;
            text-decoration: none;
            font-size: 0.9em;
            transition: background 0.2s;
            border: none;
            cursor: pointer;
        }
        
        .logout-btn:hover {
            background: #c53030;
        }
        
        /* System Menu Modal */
        .system-modal {
            display: none;
            position: fixed;
            z-index: 999999;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            background-color: rgba(0,0,0,0.5);
            backdrop-filter: blur(5px);
        }
        
        .system-modal-content {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            margin: 5% auto;
            padding: 0;
            border-radius: 20px;
            width: 90%;
            max-width: 600px;
            max-height: 85vh;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            animation: slideDown 0.3s ease-out;
            overflow: hidden;
            display: flex;
            flex-direction: column;
        }
        
        .system-modal-header {
            padding: 30px;
            color: white;
            border-bottom: 1px solid rgba(255,255,255,0.2);
            flex-shrink: 0;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .system-modal-header h2 {
            margin: 0;
            font-size: 28px;
            font-weight: 700;
        }
        
        .system-modal-close {
            background: none;
            border: none;
            color: white;
            font-size: 28px;
            cursor: pointer;
            padding: 0;
            width: 40px;
            height: 40px;
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 50%;
            transition: all 0.2s;
        }
        
        .system-modal-close:hover {
            background: rgba(255,255,255,0.2);
            transform: rotate(90deg);
        }
        
        .system-modal-body {
            padding: 30px;
            background: white;
            border-radius: 0 0 20px 20px;
            overflow-y: auto;
            flex-grow: 1;
        }
        
        .system-modal-body::-webkit-scrollbar {
            width: 8px;
        }
        
        .system-modal-body::-webkit-scrollbar-track {
            background: #f1f1f1;
            border-radius: 10px;
        }
        
        .system-modal-body::-webkit-scrollbar-thumb {
            background: #667eea;
            border-radius: 10px;
        }
        
        .system-modal-body::-webkit-scrollbar-thumb:hover {
            background: #764ba2;
        }
        
        .system-action-btn {
            width: 100%;
            margin: 10px 0;
            padding: 16px 24px;
            font-size: 15px;
            text-align: left;
            display: flex;
            align-items: center;
            gap: 15px;
            background: #f8f9fa;
            border: 1px solid #e2e8f0;
            color: #2d3748;
            transition: all 0.2s;
        }
        
        .system-action-btn:hover {
            background: #fff;
            border-color: #667eea;
            transform: translateY(-1px);
            box-shadow: 0 4px 12px rgba(102, 126, 234, 0.15);
        }
        
        .system-action-btn i {
            width: 30px;
            font-size: 18px;
            color: #667eea;
        }
        
        .system-action-btn.btn-danger {
            background: #fff5f5;
            border-color: #feb2b2;
            color: #c53030;
        }
        
        .system-action-btn.btn-danger i {
            color: #e53e3e;
        }
        
        .system-action-btn.btn-danger:hover {
            background: #fff;
            border-color: #fc8181;
            box-shadow: 0 4px 12px rgba(229, 62, 62, 0.15);
        }
        
        .system-action-btn.btn-success {
            background: #f0fff4;
            border-color: #9ae6b4;
        }
        
        .system-action-btn.btn-success i {
            color: #38a169;
        }
        
        .system-action-btn.btn-success:hover {
            border-color: #68d391;
            box-shadow: 0 4px 12px rgba(56, 161, 105, 0.15);
        }
        
        /* Theme Card Styles */
        .theme-card {
            border: 2px solid #e2e8f0;
            border-radius: 15px;
            overflow: hidden;
            cursor: pointer;
            transition: all 0.3s ease;
            background: white;
            position: relative;
        }
        .theme-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 10px 25px rgba(0,0,0,0.1);
            border-color: #667eea;
        }
        .theme-card.active {
            border-color: #38a169;
            box-shadow: 0 0 0 3px rgba(56, 161, 105, 0.2);
        }
        .theme-card-image {
            width: 100%;
            height: 150px;
            object-fit: cover;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-size: 48px;
        }
        .theme-card-body {
            padding: 15px;
        }
        .theme-card-title {
            font-weight: 600;
            color: #2d3748;
            margin-bottom: 5px;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .theme-card-meta {
            font-size: 0.85em;
            color: #718096;
            margin-bottom: 10px;
        }
        .theme-card-actions {
            display: flex;
            gap: 8px;
            margin-top: 10px;
        }
        .theme-card-actions button {
            flex: 1;
            padding: 8px;
            font-size: 0.9em;
        }
        .theme-badge {
            position: absolute;
            top: 10px;
            right: 10px;
            background: #38a169;
            color: white;
            padding: 5px 12px;
            border-radius: 20px;
            font-size: 0.85em;
            font-weight: 600;
        }
        
        .status-bar {
            display: flex;
            gap: 15px;
            flex-wrap: wrap;
            justify-content: center;
            margin-top: 10px;
        }
        .status-item {
            background: rgba(255,255,255,0.9);
            padding: 10px 15px;
            border-radius: 25px;
            font-size: 0.9em;
            font-weight: 500;
        }
        .status-online { color: #38a169; }
        .status-offline { color: #e53e3e; }
        .status-warning { color: #d69e2e; }
        
        .grid { 
            display: grid; 
            grid-template-columns: repeat(auto-fit, minmax(350px, 1fr)); 
            gap: 20px;
            margin-bottom: 20px;
        }
        .card-wide {
            grid-column: span 2;
        }
        @media (max-width: 768px) {
            .card-wide {
                grid-column: span 1;
            }
        }
        .card { 
            background: rgba(255,255,255,0.95);
            backdrop-filter: blur(10px);
            border-radius: 15px;
            padding: 25px;
            box-shadow: 0 8px 32px rgba(0,0,0,0.1);
            transition: transform 0.2s ease;
        }
        .card:hover { transform: translateY(-2px); }
        .card h2 { 
            color: #2d3748;
            font-size: 1.5em;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .icon { font-size: 1.2em; }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }
        .stat-item {
            text-align: center;
            padding: 15px;
            background: rgba(255,255,255,0.7);
            border-radius: 10px;
        }
        .stat-value {
            font-size: 1.8em;
            font-weight: 700;
            color: #667eea;
        }
        .stat-label {
            font-size: 0.9em;
            color: #666;
            margin-top: 5px;
        }
        
        .stat-subtitle {
            font-size: 0.8em;
            color: #888;
            margin-top: 0.2em;
        }
        
        .form-group {
            margin-bottom: 15px;
        }
        .form-group label {
            display: block;
            margin-bottom: 5px;
            font-weight: 600;
            color: #2d3748;
        }
        .form-group input, .form-group select {
            width: 100%;
            padding: 12px;
            border: 2px solid #e2e8f0;
            border-radius: 8px;
            font-size: 1em;
            transition: border-color 0.2s ease;
        }
        .form-group input:focus, .form-group select:focus {
            outline: none;
            border-color: #667eea;
        }
        
        .btn {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 14px 28px;
            border-radius: 12px;
            font-size: 15px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 10px;
            margin: 8px;
            box-shadow: 0 4px 15px rgba(102, 126, 234, 0.3);
            position: relative;
            overflow: hidden;
        }
        
        .btn::before {
            content: '';
            position: absolute;
            top: 0;
            left: -100%;
            width: 100%;
            height: 100%;
            background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.2), transparent);
            transition: left 0.5s;
        }
        
        .btn:hover::before {
            left: 100%;
        }
        
        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 8px 25px rgba(102, 126, 234, 0.4);
        }
        
        .btn:active {
            transform: translateY(0);
            box-shadow: 0 4px 15px rgba(102, 126, 234, 0.3);
        }
        .btn-primary {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        }
        .btn-success {
            background: linear-gradient(135deg, #4CAF50 0%, #45a049 100%);
        }
        .btn-warning {
            background: linear-gradient(135deg, #ff9800 0%, #f57c00 100%);
        }
        .btn-danger {
            background: linear-gradient(135deg, #f44336 0%, #d32f2f 100%);
        }
        .btn-info {
            background: linear-gradient(135deg, #2196F3 0%, #1976D2 100%);
        }
        .btn-danger:hover {
            background: linear-gradient(135deg, #c53030 0%, #9c2626 100%);
        }
        
        .signal-excellent {
            color: #38a169;
            background: rgba(56, 161, 105, 0.1);
            padding: 10px 20px;
            border-radius: 8px;
            border: 2px solid rgba(56, 161, 105, 0.3);
        }
        .signal-good {
            color: #2b6cb0;
            background: rgba(43, 108, 176, 0.1);
            padding: 10px 20px;
            border-radius: 8px;
            border: 2px solid rgba(43, 108, 176, 0.3);
        }
        .signal-fair {
            color: #d69e2e;
            background: rgba(214, 158, 46, 0.1);
            padding: 10px 20px;
            border-radius: 8px;
            border: 2px solid rgba(214, 158, 46, 0.3);
        }
        .signal-poor {
            color: #e53e3e;
            background: rgba(229, 62, 62, 0.1);
            padding: 10px 20px;
            border-radius: 8px;
            border: 2px solid rgba(229, 62, 62, 0.3);
        }
        .signal-none {
            color: #718096;
            background: rgba(113, 128, 150, 0.1);
            padding: 10px 20px;
            border-radius: 8px;
            border: 2px solid rgba(113, 128, 150, 0.3);
        }
        
        .signal-bars {
            display: inline-block;
            margin-right: 5px;
        }
        
        .signal-bar {
            display: inline-block;
            width: 4px;
            height: 12px;
            margin-right: 1px;
            background-color: #e2e8f0;
            border-radius: 1px;
        }
        
        .signal-bar.filled {
            background-color: #e53e3e; /* Red by default */
        }
        
        .signal-bar.filled.excellent {
            background-color: #38a169; /* Green */
        }
        
        .signal-bar.filled.good {
            background-color: #68d391; /* Light green */
        }
        
        .signal-bar.filled.fair {
            background-color: #f6ad55; /* Orange */
        }
        
        .signal-bar.filled.poor {
            background-color: #fc8181; /* Light red */
        }
        
        .table-container {
            max-height: 300px;
            overflow-y: auto;
            border-radius: 8px;
            border: 1px solid #e2e8f0;
            margin-top: 15px;
        }
        .table-container::-webkit-scrollbar {
            width: 8px;
        }
        .table-container::-webkit-scrollbar-track {
            background: #f1f5f9;
            border-radius: 4px;
        }
        .table-container::-webkit-scrollbar-thumb {
            background: #667eea;
            border-radius: 4px;
        }
        .table-container::-webkit-scrollbar-thumb:hover {
            background: #5a67d8;
        }
        .table {
            width: 100%;
            border-collapse: collapse;
            margin: 0;
        }
        .table th, .table td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #e2e8f0;
            white-space: nowrap;
        }
        .table th {
            background: rgba(102, 126, 234, 0.1);
            font-weight: 600;
            color: #2d3748;
            position: sticky;
            top: 0;
            z-index: 10;
        }
        .table tr:hover {
            background: rgba(102, 126, 234, 0.05);
        }
        .table td:first-child {
            font-family: 'Courier New', monospace;
            font-size: 0.9em;
        }
        .table td:nth-child(2) {
            font-family: 'Courier New', monospace;
            font-size: 0.85em;
            color: #718096;
        }
        
        .alert {
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 20px;
            font-weight: 500;
        }
        .alert-success {
            background: rgba(56, 161, 105, 0.1);
            color: #2f855a;
            border: 1px solid rgba(56, 161, 105, 0.2);
        }
        .alert-error {
            background: rgba(229, 62, 62, 0.1);
            color: #c53030;
            border: 1px solid rgba(229, 62, 62, 0.2);
        }
        
        .loading {
            display: inline-block;
            width: 20px;
            height: 20px;
            border: 3px solid rgba(255,255,255,.3);
            border-radius: 50%;
            border-top-color: #fff;
            animation: spin 1s ease-in-out infinite;
        }
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        
        .toggle-switch {
            position: relative;
            display: inline-block;
            width: 60px;
            height: 34px;
        }
        .toggle-switch input {
            opacity: 0;
            width: 0;
            height: 0;
        }
        .slider {
            position: absolute;
            cursor: pointer;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background-color: #ccc;
            transition: .4s;
            border-radius: 34px;
        }
        .slider:before {
            position: absolute;
            content: "";
            height: 26px;
            width: 26px;
            left: 4px;
            bottom: 4px;
            background-color: white;
            transition: .4s;
            border-radius: 50%;
        }
        input:checked + .slider {
            background-color: #667eea;
        }
        input:checked + .slider:before {
            transform: translateX(26px);
        }
        
        @media (max-width: 768px) {
            .container { padding: 10px; }
            .grid { grid-template-columns: 1fr; }
            .header h1 { font-size: 2em; }
        }
        
        /* Modal Styles */
        .modal {
            display: none;
            position: fixed;
            z-index: 1000;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            background-color: rgba(0,0,0,0.5);
            backdrop-filter: blur(5px);
        }
        .modal-content {
            background: rgba(255,255,255,0.95);
            backdrop-filter: blur(10px);
            margin: 5% auto;
            padding: 20px;
            border-radius: 15px;
            width: 90%;
            max-width: 600px;
            max-height: 80vh;
            overflow-y: auto;
            box-shadow: 0 8px 32px rgba(0,0,0,0.3);
        }
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding-bottom: 10px;
            border-bottom: 2px solid #e2e8f0;
        }
        .modal-header h2 {
            margin: 0;
            color: #2d3748;
        }
        .close {
            color: #aaa;
            font-size: 28px;
            font-weight: bold;
            cursor: pointer;
            border: none;
            background: none;
        }
        .close:hover {
            color: #000;
        }
        .network-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 15px;
            margin: 10px 0;
            background: rgba(255,255,255,0.7);
            border-radius: 10px;
            border: 1px solid #e2e8f0;
            transition: all 0.2s ease;
        }
        .network-item:hover {
            background: rgba(102, 126, 234, 0.1);
            transform: translateY(-1px);
        }
        .network-item.connected {
            background: rgba(56, 161, 105, 0.1);
            border: 2px solid #38a169;
        }
        .network-item.connected .network-name {
            color: #38a169;
            font-weight: 700;
        }
        .network-info {
            flex-grow: 1;
        }
        .network-name {
            font-weight: 600;
            color: #2d3748;
            margin-bottom: 5px;
        }
        .network-details {
            font-size: 0.9em;
            color: #666;
        }
        .network-actions {
            display: flex;
            gap: 10px;
            align-items: center;
        }
        .network-actions input {
            padding: 8px;
            border: 1px solid #e2e8f0;
            border-radius: 5px;
            width: 150px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div style="display: flex; justify-content: space-between; align-items: center; width: 100%;">
                <h1 style="margin: 0;">🚀 OpenPiRouter Dashboard</h1>
                <div style="display: flex; gap: 15px; align-items: center;">
                    <span style="color: #666; font-size: 14px; font-weight: 500;" id="uptime-display">⏱️ {{ system_status.uptime }}</span>
                    <button onclick="openThemeModal()" style="background: none; border: none; font-size: 20px; cursor: pointer; padding: 8px; transition: transform 0.2s; color: #666;" onmouseover="this.style.transform='scale(1.2)'" onmouseout="this.style.transform='scale(1)'"><i class="fas fa-palette"></i></button>
                    <button onclick="openSystemModal()" style="background: none; border: none; font-size: 20px; cursor: pointer; padding: 8px; transition: transform 0.2s; color: #666;" onmouseover="this.style.transform='scale(1.2)'" onmouseout="this.style.transform='scale(1)'"><i class="fas fa-cog"></i></button>
                    <a href="/logout" style="background: none; border: none; font-size: 20px; cursor: pointer; text-decoration: none; padding: 8px; transition: transform 0.2s; color: #666;" onmouseover="this.style.transform='scale(1.2)'" onmouseout="this.style.transform='scale(1)'"><i class="fas fa-sign-out-alt"></i></a>
                </div>
            </div>
            <div class="status-bar">
                <div class="status-item {{ 'status-online' if system_status.wifi else 'status-offline' }}">
                    📶 WLAN (wlan0): {{ 'Online' if system_status.wifi else 'Offline' }}
                </div>
                <div class="status-item {{ 'status-online' if system_status.internet else 'status-offline' }}">
                    🌐 Internet: {{ 'Verbunden' if system_status.internet else 'Getrennt' }}
                </div>
                <div class="status-item status-online" id="speed-status" style="min-width: 195px;">
                    📊 <span id="download-speed">0.0</span> ↓ <span id="upload-speed">0.0</span> ↑ Mbit/s
                </div>
                <div class="status-item {{ 'status-online' if system_status.ap else 'status-offline' }}">
                    📡 AP (wlan1): {{ 'Online' if system_status.ap else 'Offline' }}
                </div>
                <div class="status-item {{ 'status-online' if system_status.pihole else 'status-offline' }}">
                    🛡️ Pi-hole: {{ 'Online' if system_status.pihole else 'Offline' }}
                </div>
                <div class="status-item status-warning" id="websocket-status">
                    🔗 WS: Verbinde...
                </div>
            </div>
        </div>

        {% if messages %}
            {% for message in messages %}
                <div class="alert alert-{{ message.type }}">{{ message.text }}</div>
            {% endfor %}
        {% endif %}

        <div class="grid">
            <!-- System Status -->
            <div class="card">
                <h2><span class="icon">📊</span>System Status</h2>
                <div class="stats-grid">
                    <div class="stat-item">
                        <div class="stat-value">{{ system_stats.cpu }}%</div>
                        <div class="stat-label">CPU</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value">{{ system_stats.memory }}%</div>
                        <div class="stat-label">RAM</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value">{{ "%.0f"|format((system_stats.disk_used / system_stats.disk_total) * 100) }}%</div>
                        <div class="stat-label">Speicher</div>
                        <div class="stat-subtitle">Belegt</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value">{{ system_stats.temperature }}°C</div>
                        <div class="stat-label">Temp</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value">{{ system_stats.clients }}</div>
                        <div class="stat-label">Clients</div>
                    </div>
                </div>
            </div>

            <!-- Internet Connection -->
            <div class="card">
                <h2><span class="icon">🌐</span>Internet Verbindung (wlan0)</h2>
                
                <!-- Current Connection -->
                <div id="current-wifi-section" style="display: none;">
                    <div class="form-group">
                        <label>Aktuelle Verbindung:</label>
                        <div style="display: flex; align-items: center; gap: 15px;">
                            <input type="text" id="current_ssid" readonly style="background: #f7fafc; flex: 1;">
                            <div id="signal-strength" style="display: flex; align-items: center; gap: 5px;">
                                <span id="signal-bars"></span>
                                <span id="signal-value">0%</span>
                            </div>
                        </div>
                    </div>
                    <div style="display: flex; gap: 10px; margin-bottom: 20px;">
                        <button class="btn btn-danger" onclick="disconnectWifi()">Trennen</button>
                        <button class="btn" onclick="showWifiModal()">WLAN Scannen</button>
                    </div>
                    <hr style="margin: 20px 0;">
                </div>
                
                <!-- Signal Status Display -->
                <div id="signal-status-section" style="text-align: center; padding: 20px; margin: 20px 0;">
                    <div id="signal-status-text" style="font-size: 1.2em; font-weight: 600;">Keine WLAN-Verbindung</div>
                </div>
            </div>

            <!-- Access Point (wlan1) -->
            <div class="card">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
                    <h2 style="margin: 0;"><span class="icon">📡</span>Access Point (wlan1)</h2>
                    <div style="display: flex; align-items: center; gap: 10px;">
                        <span class="ssid-visibility-status" style="font-size: 14px; font-weight: 600;">SSID {{ 'Öffentlich' if ap.ssid_visible else 'Versteckt' }}</span>
                        <label class="toggle-switch">
                            <input type="checkbox" id="ap_visibility_toggle" {{ 'checked' if ap.ssid_visible else '' }} onchange="toggleAPVisibility()">
                            <span class="slider"></span>
                        </label>
                    </div>
                </div>
                <div class="form-group">
                    <label>SSID:</label>
                    <input type="text" id="ap_ssid" value="{{ ap.ssid }}" placeholder="AP Name">
                </div>
                <div class="form-group">
                    <label>Passwort:</label>
                    <input type="password" id="ap_pass" value="{{ ap.password }}" placeholder="AP Passwort">
                </div>
                <div class="form-group">
                    <label>Band:</label>
                    <select id="ap_band">
                        <option value="2G" {{ 'selected' if ap.band == '2G' else '' }}>2.4 GHz</option>
                        <option value="5G" {{ 'selected' if ap.band == '5G' else '' }}>5 GHz</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Kanal:</label>
                    <select id="ap_channel">
                        {% for ch in available_channels %}
                        <option value="{{ ch }}" {{ 'selected' if ch|string == ap.channel|string else '' }}>{{ ch }}</option>
                        {% endfor %}
                    </select>
                </div>
                
                <!-- Button Grid -->
                <div style="display: flex; gap: 10px; margin: 20px 0; flex-wrap: wrap;">
                    <button class="btn btn-primary" onclick="updateAP()" style="flex: 1; min-width: 140px;">Speichern</button>
                    <button class="btn btn-success" onclick="showQRCode()" style="flex: 1; min-width: 140px;">QR-Code</button>
                    <button class="btn btn-warning" onclick="restartAP()" style="flex: 1; min-width: 140px;">Neustart</button>
                </div>
            </div>

            <!-- Internet Konfiguration -->
            <div class="card">
                <h2><span class="icon">🌐</span>Internet Konfiguration</h2>
                <p style="color: #666; margin-bottom: 20px;">
                    wlan0 = Internet-Empfang | RJ45 = Internet-Empfang ODER Internet-Ausgabe (wie wlan1)
                </p>
                
                <div class="form-group">
                    <label>wlan0 Internet-Empfang:</label>
                    <label class="toggle-switch">
                        <input type="checkbox" id="wlan0_internet_toggle" {{ 'checked' if wlan0_internet_enabled else '' }} onchange="toggleWlan0Internet()">
                        <span class="slider"></span>
                    </label>
                    <span style="margin-left: 10px;">wlan0 Internet-Empfang {{ 'Aktiviert' if wlan0_internet_enabled else 'Deaktiviert' }}</span>
                </div>
                
                <div class="form-group">
                    <label>RJ45 Modus:</label>
                    <select id="eth0_mode">
                        <option value="receive" {{ 'selected' if eth0_mode == 'receive' else '' }}>Internet-Empfang (wie wlan0)</option>
                        <option value="output" {{ 'selected' if eth0_mode == 'output' else '' }}>Internet-Ausgabe (wie wlan1)</option>
                    </select>
                </div>
                
                <button class="btn" onclick="updateEth0Mode()">RJ45 Modus Aktualisieren</button>
            </div>

            <!-- Pi-hole -->
            <div class="card">
                <h2><span class="icon">🛡️</span>Pi-hole DNS Filter</h2>
                <div class="stats-grid">
                    <div class="stat-item">
                        <div class="stat-value" id="pihole-queries">{{ system_stats.pihole_queries }}</div>
                        <div class="stat-label">DNS Anfragen</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value" id="pihole-blocked">{{ system_stats.pihole_blocked }}</div>
                        <div class="stat-label">Geblockt</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value" id="pihole-percent">{{ system_stats.pihole_blocked_percent }}%</div>
                        <div class="stat-label">Block Rate</div>
                    </div>
                </div>
                <div style="margin: 15px 0;">
                    <label class="toggle-switch">
                        <input type="checkbox" id="pihole_toggle" {{ 'checked' if system_status.pihole else '' }} onchange="togglePiHole()">
                        <span class="slider"></span>
                    </label>
                    <span style="margin-left: 10px;">Pi-hole {{ 'Aktiviert' if system_status.pihole else 'Deaktiviert' }}</span>
                </div>
                <button class="btn" onclick="openPiHoleAdmin()">Pi-hole Admin</button>
            </div>

            <!-- Connected Clients -->
            <div class="card card-wide">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
                    <h2 style="margin: 0;"><span class="icon">👥</span>Verbundene Geräte</h2>
                    <button class="btn btn-warning" onclick="cleanupOldClients()" style="margin: 0;">
                        <i class="fas fa-broom"></i> Alte Clients entfernen
                    </button>
                </div>
                {% if clients %}
                <div class="table-container">
                    <table class="table">
                        <thead>
                            <tr><th>IP</th><th>MAC</th><th>Hostname</th><th>Signal</th><th>Download</th><th>Upload</th></tr>
                        </thead>
                        <tbody>
                            {% for client in clients %}
                            <tr>
                                <td>{{ client.ip }}</td>
                                <td>{{ client.mac }}</td>
                                <td>{{ client.hostname or '-' }}</td>
                                <td>{{ client.signal or '-' }} {% if client.interface == 'br0' %}(LAN){% endif %}</td>
                                <td><span class="client-speed" data-mac="{{ client.mac }}" data-type="download">0.0</span> Mbit/s</td>
                                <td><span class="client-speed" data-mac="{{ client.mac }}" data-type="upload">0.0</span> Mbit/s</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
                {% else %}
                <p>Keine verbundenen Geräte</p>
                {% endif %}
            </div>

        </div>
    </div>

    <!-- WLAN Scan Modal -->
    <div id="wifiModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2>🔍 Verfügbare WLAN-Netzwerke</h2>
                <button class="close" onclick="closeWifiModal()">&times;</button>
            </div>
            <div id="wifiNetworks">
                <div style="text-align: center; padding: 20px;">
                    <div class="loading"></div>
                    <p>Suche nach WLAN-Netzwerken...</p>
                </div>
            </div>
        </div>
    </div>

    <!-- System Actions Modal -->
    <div id="systemModal" class="system-modal">
        <div class="system-modal-content">
            <div class="system-modal-header">
                <h2><i class="fas fa-cog"></i> System Aktionen</h2>
                <button class="system-modal-close" onclick="closeSystemModal()">
                    <i class="fas fa-times"></i>
                </button>
            </div>
            <div class="system-modal-body">
                <button class="system-action-btn" onclick="restartService('hostapd')">
                    <i class="fas fa-sync-alt"></i>
                    <span>HostAPD Neustart</span>
                </button>
                
                <button class="system-action-btn" onclick="restartService('dnsmasq')">
                    <i class="fas fa-sync-alt"></i>
                    <span>DNS Neustart</span>
                </button>
                
                <button class="system-action-btn" onclick="restartService('pihole-FTL')">
                    <i class="fas fa-sync-alt"></i>
                    <span>Pi-hole Neustart</span>
                </button>
                
                <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 20px 0;">
                
                <button class="system-action-btn btn-success" onclick="exportConfig()">
                    <i class="fas fa-download"></i>
                    <span>Export Konfiguration</span>
                </button>
                
                <button class="system-action-btn btn-success" onclick="document.getElementById('config_file').click()">
                    <i class="fas fa-upload"></i>
                    <span>Import Konfiguration</span>
                </button>
                <input type="file" id="config_file" accept=".yaml,.yml" style="display: none;" onchange="importConfig()">
                
                <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 20px 0;">
                
                <button class="system-action-btn btn-danger" onclick="rebootSystem()">
                    <i class="fas fa-power-off"></i>
                    <span>System Neustart</span>
                </button>
            </div>
        </div>
    </div>

    <!-- Theme Manager Modal -->
    <div id="themeModal" class="system-modal">
        <div class="system-modal-content" style="max-width: 900px;">
            <div class="system-modal-header">
                <h2><i class="fas fa-palette"></i> Theme Manager</h2>
                <button class="system-modal-close" onclick="closeThemeModal()">
                    <i class="fas fa-times"></i>
                </button>
            </div>
            <div class="system-modal-body">
                <!-- Theme Actions -->
                <div style="display: flex; gap: 10px; margin-bottom: 20px;">
                    <button class="btn btn-success" onclick="exportCurrentTheme()">
                        <i class="fas fa-download"></i> Aktuelles Theme Exportieren
                    </button>
                    <button class="btn btn-primary" onclick="document.getElementById('theme_upload').click()">
                        <i class="fas fa-upload"></i> Theme Hochladen
                    </button>
                    <input type="file" id="theme_upload" accept=".zip" style="display: none;" onchange="uploadTheme(this)">
                </div>
                
                <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 20px 0;">
                
                <h3 style="margin-bottom: 15px;">Verfügbare Themes</h3>
                
                <!-- Themes Grid -->
                <div id="themesGrid" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 15px;">
                    <!-- Themes will be loaded here -->
                    <div style="text-align: center; padding: 40px; color: #999; grid-column: 1/-1;">
                        Lade Themes...
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Socket.IO Client Library -->
    <script src="https://cdn.socket.io/4.7.2/socket.io.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    
    <script>
        // Speed monitoring variables
        let lastSpeedData = null;
        let speedCheckCount = 0;
        
        // WebSocket connection
        const socket = io();
        
        // WebSocket event handlers
        socket.on('connect', function() {
            console.log('✅ Connected to OpenPiRouter Dashboard via WebSocket');
            
            // Update WebSocket status in header
            const wsStatus = document.getElementById('websocket-status');
            if (wsStatus) {
                wsStatus.textContent = '🔗 WS: Verbunden';
                wsStatus.className = 'status-item status-online';
            }
        });
        
        socket.on('disconnect', function() {
            console.log('❌ Disconnected from dashboard');
            
            // Update WebSocket status in header
            const wsStatus = document.getElementById('websocket-status');
            if (wsStatus) {
                wsStatus.textContent = '🔗 WS: Getrennt';
                wsStatus.className = 'status-item status-offline';
            }
        });
        
        socket.on('system_status', function(data) {
            console.log('📊 Received system status:', data);
            updateSystemStatus(data);
        });
        
        socket.on('system_stats', function(data) {
            console.log('📈 Received system stats:', data);
            updateSystemStats(data);
        });
        
        socket.on('wifi_status', function(data) {
            console.log('📶 Received WiFi status:', data);
            updateWiFiStatus(data);
        });
        
        socket.on('speed_data', function(data) {
            console.log('⚡ Received speed data:', data);
            updateSpeedData(data);
        });
        
        socket.on('client_list', function(data) {
            console.log('👥 Received client list:', data);
            updateClientList(data);
        });
        
        // Update functions
        function updateClientList(data) {
            if (!data || !data.clients) return;
            
            // Update client count in stats
            const statsGrid = document.querySelector('.stats-grid');
            if (statsGrid) {
                const statItems = statsGrid.querySelectorAll('.stat-item');
                if (statItems[4]) {
                    statItems[4].querySelector('.stat-value').textContent = data.count;
                }
            }
            
            // Update client table
            const tbody = document.querySelector('.table tbody');
            if (tbody) {
                tbody.innerHTML = '';
                data.clients.forEach(client => {
                    const row = document.createElement('tr');
                    row.innerHTML = `
                        <td>${client.ip}</td>
                        <td>${client.mac}</td>
                        <td>${client.hostname || '-'}</td>
                        <td>${client.signal || '-'}${client.interface == 'br0' ? ' (LAN)' : ''}</td>
                        <td><span class="client-speed" data-mac="${client.mac}" data-type="download">0.0</span> Mbit/s</td>
                        <td><span class="client-speed" data-mac="${client.mac}" data-type="upload">0.0</span> Mbit/s</td>
                    `;
                    tbody.appendChild(row);
                });
                
                // Show/hide "No clients" message
                const noClients = document.querySelector('.table-container + p');
                if (noClients) {
                    noClients.style.display = data.count > 0 ? 'none' : 'block';
                }
            }
        }
        
        function updateSystemStatus(status) {
            // Update status indicators
            const wifiStatus = document.querySelector('.status-item:nth-child(1)');
            const internetStatus = document.querySelector('.status-item:nth-child(2)');
            const apStatus = document.querySelector('.status-item:nth-child(4)');
            const piholeStatus = document.querySelector('.status-item:nth-child(5)');
            
            if (wifiStatus) {
                wifiStatus.textContent = `📶 WLAN (wlan0): ${status.wifi ? 'Online' : 'Offline'}`;
                wifiStatus.className = `status-item ${status.wifi ? 'status-online' : 'status-offline'}`;
            }
            
            if (internetStatus) {
                internetStatus.textContent = `🌐 Internet: ${status.internet ? 'Verbunden' : 'Getrennt'}`;
                internetStatus.className = `status-item ${status.internet ? 'status-online' : 'status-offline'}`;
            }
            
            if (apStatus) {
                apStatus.textContent = `📡 AP (wlan1): ${status.ap ? 'Online' : 'Offline'}`;
                apStatus.className = `status-item ${status.ap ? 'status-online' : 'status-offline'}`;
            }
            
            if (piholeStatus) {
                piholeStatus.textContent = `🛡️ Pi-hole: ${status.pihole ? 'Online' : 'Offline'}`;
                piholeStatus.className = `status-item ${status.pihole ? 'status-online' : 'status-offline'}`;
            }
            
            // Update uptime in header
            const uptimeDisplay = document.getElementById('uptime-display');
            if (uptimeDisplay && status.uptime) {
                uptimeDisplay.textContent = `⏱️ ${status.uptime}`;
            }
        }
        
        function updateSystemStats(stats) {
            // Update stats grid elements
            const statsGrid = document.querySelector('.stats-grid');
            if (statsGrid) {
                const statItems = statsGrid.querySelectorAll('.stat-item');
                if (statItems.length >= 4) {
                    // CPU
                    statItems[0].querySelector('.stat-value').textContent = `${stats.cpu}%`;
                    // RAM
                    statItems[1].querySelector('.stat-value').textContent = `${stats.memory}%`;
                    // Speicher (Disk)
                    const diskPercent = Math.round((stats.disk_used / stats.disk_total) * 100);
                    statItems[2].querySelector('.stat-value').textContent = `${diskPercent}%`;
                    const subtitle = statItems[2].querySelector('.stat-subtitle');
                    if (subtitle) {
                        subtitle.textContent = 'Belegt';
                    }
                    // Temperatur
                    statItems[3].querySelector('.stat-value').textContent = `${stats.temperature}°C`;
                    // Clients
                    if (statItems[4]) {
                        statItems[4].querySelector('.stat-value').textContent = stats.clients;
                    }
                }
            }
            
            // Update uptime if element exists
            const uptimeEl = document.getElementById('uptime');
            if (uptimeEl && stats.uptime) {
                uptimeEl.textContent = stats.uptime;
            }
            
            // Update Pi-hole statistics
            const piholeQueries = document.getElementById('pihole-queries');
            const piholeBlocked = document.getElementById('pihole-blocked');
            const piholePercent = document.getElementById('pihole-percent');
            
            if (piholeQueries) piholeQueries.textContent = stats.pihole_queries || 0;
            if (piholeBlocked) piholeBlocked.textContent = stats.pihole_blocked || 0;
            if (piholePercent) piholePercent.textContent = (stats.pihole_blocked_percent || 0) + '%';
        }
        
        function updateWiFiStatus(wifiData) {
            if (wifiData.connected) {
                document.getElementById('current_ssid').value = wifiData.ssid;
                document.getElementById('signal-value').textContent = wifiData.signal + '%';
                document.getElementById('current-wifi-section').style.display = 'block';
                
                // Update signal bars
                const signalBarsContainer = document.getElementById('signal-bars');
                signalBarsContainer.innerHTML = '';
                signalBarsContainer.appendChild(createSignalBars(wifiData.signal));
                
                // Update signal status display
                const quality = getSignalQuality(wifiData.signal);
                const statusElement = document.getElementById('signal-status-text');
                statusElement.textContent = quality.text;
                statusElement.className = quality.class;
            } else {
                document.getElementById('current-wifi-section').style.display = 'none';
                
                // Show no connection status
                const statusElement = document.getElementById('signal-status-text');
                statusElement.textContent = 'Keine WLAN-Verbindung';
                statusElement.className = 'signal-none';
            }
        }
        
        // Moving average for smoother speed display
        let speedHistory = {
            download: [],
            upload: []
        };
        const HISTORY_SIZE = 3; // Average over last 3 measurements
        
        function updateSpeedData(data) {
            if (!data || !data.success) {
                return;
            }
            
            const wanRx = parseInt(data.wan_rx) || 0;
            const apTx = parseInt(data.ap_tx) || 0;
            const currentTime = parseInt(data.timestamp) || Date.now();
            
            const downloadEl = document.getElementById('download-speed');
            const uploadEl = document.getElementById('upload-speed');
            
            if (!downloadEl || !uploadEl) {
                return;
            }
            
            if (lastSpeedData && lastSpeedData.wan_rx !== undefined) {
                const timeDiff = (currentTime - lastSpeedData.timestamp) / 1000;
                const wanDiff = wanRx - lastSpeedData.wan_rx;
                const apDiff = apTx - lastSpeedData.ap_tx;
                
                if (timeDiff > 1 && timeDiff < 10 && wanDiff >= 0 && apDiff >= 0) {
                    // Convert bytes to Mbit/s
                    let downloadMbps = (wanDiff * 8) / (timeDiff * 1024 * 1024);
                    let uploadMbps = (apDiff * 8) / (timeDiff * 1024 * 1024);
                    
                    // Add to history
                    speedHistory.download.push(downloadMbps);
                    speedHistory.upload.push(uploadMbps);
                    
                    // Keep only last N measurements
                    if (speedHistory.download.length > HISTORY_SIZE) {
                        speedHistory.download.shift();
                        speedHistory.upload.shift();
                    }
                    
                    // Calculate moving average
                    const avgDownload = speedHistory.download.reduce((a, b) => a + b, 0) / speedHistory.download.length;
                    const avgUpload = speedHistory.upload.reduce((a, b) => a + b, 0) / speedHistory.upload.length;
                    
                    // Filter very small values (< 0.1 Mbit/s)
                    const displayDownload = avgDownload < 0.1 ? 0 : Math.round(avgDownload * 10) / 10;
                    const displayUpload = avgUpload < 0.1 ? 0 : Math.round(avgUpload * 10) / 10;
                    
                    downloadEl.textContent = displayDownload.toFixed(1);
                    uploadEl.textContent = displayUpload.toFixed(1);
                }
            } else {
                downloadEl.textContent = '0.0';
                uploadEl.textContent = '0.0';
            }
            
            lastSpeedData = {
                wan_rx: wanRx,
                ap_tx: apTx,
                timestamp: currentTime
            };
        }
        
        // Get signal quality text and class
        function getSignalQuality(signal) {
            const signalNum = parseInt(signal);
            if (signalNum >= 95) {
                return { text: '🏆 Ausgezeichnet', class: 'signal-excellent', color: 'excellent' };
            } else if (signalNum >= 80) {
                return { text: '✅ Sehr gut', class: 'signal-good', color: 'good' };
            } else if (signalNum >= 60) {
                return { text: '👍 Gut', class: 'signal-fair', color: 'fair' };
            } else if (signalNum >= 30) {
                return { text: '⚠️ Schwach', class: 'signal-poor', color: 'poor' };
            } else {
                return { text: '❌ Sehr schwach', class: 'signal-poor', color: 'poor' };
            }
        }
        
        function createSignalBars(signal) {
            const bars = [];
            const quality = getSignalQuality(signal);
            const filledBars = Math.ceil(signal / 25); // 4 bars total, each represents 25%
            
            for (let i = 0; i < 4; i++) {
                const bar = document.createElement('span');
                bar.className = 'signal-bar';
                if (i < filledBars) {
                    bar.classList.add('filled');
                    bar.classList.add(quality.color);
                }
                bars.push(bar);
            }
            
            const container = document.createElement('span');
            container.className = 'signal-bars';
            bars.forEach(bar => container.appendChild(bar));
            return container;
        }
        
        function connectWifi() {
            const ssid = document.getElementById('wan_ssid').value;
            const pass = document.getElementById('wan_pass').value;
            
            fetch('/api/connect_wifi', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ssid: ssid, password: pass})
            }).then(response => response.json())
              .then(data => {
                  if (data.success) {
                      showToast('WLAN-Verbindung erfolgreich!', 'success');
                      setTimeout(() => location.reload(), 2000);
                  } else {
                      showToast('WLAN-Verbindung fehlgeschlagen: ' + data.error, 'error');
                  }
              });
        }

        function showWifiModal() {
            const modal = document.getElementById('wifiModal');
            modal.style.display = 'block';
            
            // Reset loading state
            document.getElementById('wifiNetworks').innerHTML = `
                <div style="text-align: center; padding: 20px;">
                    <div class="loading"></div>
                    <p>Suche nach WLAN-Netzwerken...</p>
                </div>
            `;
            
            // Start scan and load networks
            fetch('/api/scan_wifi')
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        loadWifiNetworks();
                    } else {
                        document.getElementById('wifiNetworks').innerHTML = `
                            <div style="text-align: center; padding: 20px; color: #e53e3e;">
                                <p>WLAN-Scan fehlgeschlagen: ${data.error || 'Unbekannter Fehler'}</p>
                            </div>
                        `;
                    }
                })
                .catch(error => {
                    document.getElementById('wifiNetworks').innerHTML = `
                        <div style="text-align: center; padding: 20px; color: #e53e3e;">
                            <p>Fehler beim Scannen: ${error.message}</p>
                        </div>
                    `;
                });
        }
        
        function closeWifiModal() {
            document.getElementById('wifiModal').style.display = 'none';
        }
        
        // Get signal quality text and class
        function getSignalQuality(signal) {
            const signalNum = parseInt(signal);
            if (signalNum >= 95) {
                return { text: '🏆 Ausgezeichnet', class: 'signal-excellent', color: 'excellent' };
            } else if (signalNum >= 80) {
                return { text: '✅ Sehr gut', class: 'signal-good', color: 'good' };
            } else if (signalNum >= 60) {
                return { text: '👍 Gut', class: 'signal-fair', color: 'fair' };
            } else if (signalNum >= 30) {
                return { text: '⚠️ Schwach', class: 'signal-poor', color: 'poor' };
            } else {
                return { text: '❌ Sehr schwach', class: 'signal-poor', color: 'poor' };
            }
        }
        
        // Load current WiFi connection on page load
        
        // Disconnect WiFi
        function disconnectWifi() {
            if (!confirm('WLAN-Verbindung wirklich trennen?')) {
                return;
            }
            
            fetch('/api/disconnect_wifi', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                }
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    alert('WLAN-Verbindung getrennt');
                    loadCurrentWifi(); // Refresh status
                    location.reload(); // Reload page to update all status
                } else {
                    alert('Fehler beim Trennen: ' + (data.error || 'Unbekannter Fehler'));
                }
            })
            .catch(error => {
                alert('Fehler beim Trennen: ' + error.message);
            });
        }
        
        function loadWifiNetworks() {
            fetch('/api/get_wifi_networks')
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        displayWifiNetworks(data.networks);
                    } else {
                        document.getElementById('wifiNetworks').innerHTML = `
                            <div style="text-align: center; padding: 20px; color: #e53e3e;">
                                <p>Fehler beim Laden der Netzwerke: ${data.error}</p>
                            </div>
                        `;
                    }
                });
        }
        
        function displayWifiNetworks(networks) {
            if (networks.length === 0) {
                document.getElementById('wifiNetworks').innerHTML = `
                    <div style="text-align: center; padding: 20px; color: #666;">
                        <p>Keine WLAN-Netzwerke gefunden</p>
                    </div>
                `;
                return;
            }
            
            // Get current connected SSID
            const currentSSID = document.getElementById('current_ssid')?.value || '';
            
            let html = '';
            networks.forEach(network => {
                const signalBars = getSignalBars(network.signal);
                const isConnected = network.ssid === currentSSID;
                const itemClass = isConnected ? 'network-item connected' : 'network-item';
                
                html += `
                    <div class="${itemClass}">
                        <div class="network-info">
                            <div class="network-name">${network.ssid || '(Versteckt)'}${isConnected ? ' ✓ Verbunden' : ''}</div>
                            <div class="network-details">
                                Signal: ${signalBars} (${network.signal}%) | 
                                Frequenz: ${network.frequency} | 
                                Sicherheit: ${network.security || 'Offen'}
                            </div>
                        </div>
                        <div class="network-actions">
                            ${isConnected ? 
                                `<button class="btn btn-danger" onclick="disconnectWifi()">Trennen</button>` :
                                `<input type="password" id="pass_${network.ssid}" placeholder="Passwort" style="display: ${network.security && network.security !== 'Offen' ? 'block' : 'none'}">
                                <button class="btn" onclick="connectToNetwork('${network.ssid}', '${network.security}')">Verbinden</button>`
                            }
                        </div>
                    </div>
                `;
            });
            
            document.getElementById('wifiNetworks').innerHTML = html;
        }
        
        function getSignalBars(signal) {
            const strength = parseInt(signal);
            if (strength >= 75) return '▂▄▆█';
            if (strength >= 50) return '▂▄▆_';
            if (strength >= 25) return '▂▄__';
            return '▂___';
        }
        
        function disconnectWifi() {
            Swal.fire({
                title: 'WLAN trennen?',
                text: 'Die Verbindung zu wlan0 wird getrennt.',
                icon: 'warning',
                showCancelButton: true,
                confirmButtonColor: '#f44336',
                cancelButtonColor: '#718096',
                confirmButtonText: 'Ja, trennen!',
                cancelButtonText: 'Abbrechen'
            }).then((result) => {
                if (result.isConfirmed) {
                    fetch('/api/disconnect_wifi', {method: 'POST'})
                        .then(response => response.json())
                        .then(data => {
                            if (data.success) {
                                showToast('WLAN getrennt', 'success');
                                setTimeout(() => location.reload(), 2000);
                            } else {
                                showToast('Fehler beim Trennen', 'error');
                            }
                        });
                }
            });
        }
        
        function connectToNetwork(ssid, security) {
            const passwordInput = document.getElementById(`pass_${ssid}`);
            const password = passwordInput ? passwordInput.value : '';
            
            if (security && security !== 'Offen' && !password) {
                showToast('Passwort erforderlich für dieses Netzwerk', 'error');
                return;
            }
            
            // Update main form
            document.getElementById('wan_ssid').value = ssid;
            if (password) {
                document.getElementById('wan_pass').value = password;
            }
            
            // Close modal and connect
            closeWifiModal();
            connectWifi();
        }
        
        // Close modal when clicking outside
        window.onclick = function(event) {
            const modal = document.getElementById('wifiModal');
            if (event.target === modal) {
                closeWifiModal();
            }
        }

        function updateAP() {
            const data = {
                ssid: document.getElementById('ap_ssid').value,
                password: document.getElementById('ap_pass').value,
                band: document.getElementById('ap_band').value,
                channel: document.getElementById('ap_channel').value
            };
            
            fetch('/api/update_ap', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            }).then(response => response.json())
              .then(data => {
                  if (data.success) {
                      showToast('✅ Access Point aktualisiert!', 'success');
                      setTimeout(() => location.reload(), 2000);
                  } else {
                      showToast('❌ Access Point Update fehlgeschlagen', 'error');
                  }
              });
        }

        function showQRCode() {
            const ssid = document.getElementById('ap_ssid').value;
            const password = document.getElementById('ap_pass').value;
            const isHidden = !document.getElementById('ap_visibility_toggle').checked;
            
            if (!ssid || !password) {
                showToast('SSID und Passwort müssen ausgefüllt sein!', 'error');
                return;
            }
            
            // Generate WIFI QR code string with correct hidden flag
            // Note: H must be 'true' or 'false' as string, not boolean
            const hiddenFlag = isHidden ? 'true' : 'false';
            const wifiString = `WIFI:T:WPA;S:${ssid};P:${password};H:${hiddenFlag};;`;
            console.log('QR-Code WiFi String:', wifiString, 'isHidden:', isHidden);
            
            // Show loading message
            Swal.fire({
                title: 'QR-Code wird generiert...',
                text: 'Bitte warten',
                allowOutsideClick: false,
                showConfirmButton: false,
                didOpen: () => {
                    Swal.showLoading();
                }
            });
            
            // Create QR code using a simple API service
            const qrApiUrl = `https://api.qrserver.com/v1/create-qr-code/?size=256x256&data=${encodeURIComponent(wifiString)}`;
            
            // Create image element
            const img = document.createElement('img');
            img.src = qrApiUrl;
            img.style.width = '256px';
            img.style.height = '256px';
            img.style.border = '1px solid #ddd';
            img.style.borderRadius = '8px';
            
            img.onload = () => {
                const hiddenBadge = isHidden ? '<span style="background: #ff9800; color: white; padding: 4px 12px; border-radius: 12px; font-size: 12px; margin-left: 10px;">Versteckt</span>' : '<span style="background: #4CAF50; color: white; padding: 4px 12px; border-radius: 12px; font-size: 12px; margin-left: 10px;">Öffentlich</span>';
                Swal.fire({
                    title: 'WLAN QR-Code',
                    html: `
                        <div style="text-align: center;">
                            <p><strong>SSID:</strong> ${ssid} ${hiddenBadge}</p>
                            <p><strong>Passwort:</strong> ${password}</p>
                            <div style="margin: 20px 0; padding: 20px; background: #f8f9fa; border-radius: 10px;">
                                ${img.outerHTML}
                            </div>
                            <p style="font-size: 14px; color: #666;">Scanne den QR-Code mit deinem Handy zum automatischen Verbinden</p>
                            ${isHidden ? '<p style="font-size: 13px; color: #ff9800; margin-top: 10px;"><strong>Hinweis:</strong> Versteckte SSID - Stelle sicher, dass dein Gerät versteckte Netzwerke unterstützt</p>' : ''}
                        </div>
                    `,
                    width: 450,
                    showConfirmButton: true,
                    confirmButtonText: 'Schließen',
                    confirmButtonColor: '#4CAF50',
                    background: '#ffffff'
                });
            };
            
            img.onerror = () => {
                Swal.close();
                showToast('QR-Code konnte nicht generiert werden', 'error');
            };
        }

        function restartAP() {
            if (confirm('Access Point wirklich neu starten?')) {
                fetch('/api/restart_ap', {method: 'POST'})
                    .then(response => response.json())
                    .then(data => {
                        showToast('Access Point wird neu gestartet...', 'success');
                        setTimeout(() => location.reload(), 3000);
                    });
            }
        }
        
        // Initialize band tracking with localStorage
        const isCurrentlyHidden = {{ 'false' if ap.ssid_visible else 'true' }};
        const currentBand = '{{ ap.band }}';
        
        // Get or initialize the saved visible band (the band to use when SSID is visible)
        let savedVisibleBand = localStorage.getItem('ap_visible_band');
        
        if (!savedVisibleBand) {
            // First time: save current band if visible, or default to 'a' if hidden
            savedVisibleBand = isCurrentlyHidden ? 'a' : currentBand;
            localStorage.setItem('ap_visible_band', savedVisibleBand);
        }
        
        console.log('Initialization:', {
            isCurrentlyHidden,
            currentBand,
            savedVisibleBand
        });
        
        function toggleAPVisibility() {
            const visible = document.getElementById('ap_visibility_toggle').checked;
            const bandSelect = document.getElementById('ap_band');
            const statusText = document.querySelector('.ssid-visibility-status');
            
            console.log('Toggle triggered:', {
                visible,
                currentBandValue: bandSelect.value,
                savedVisibleBand: localStorage.getItem('ap_visible_band')
            });
            
            // Update band dropdown based on visibility
            if (!visible) {
                // Switching to hidden: Save current band if it's not 'g'
                if (bandSelect.value !== 'g') {
                    localStorage.setItem('ap_visible_band', bandSelect.value);
                    console.log('Saved visible band:', bandSelect.value);
                }
                // Set to 2.4GHz
                bandSelect.value = 'g';
                if (statusText) statusText.textContent = 'SSID Versteckt';
            } else {
                // Switching to visible: Restore saved band
                const restoredBand = localStorage.getItem('ap_visible_band') || 'a';
                console.log('Restoring band:', restoredBand);
                bandSelect.value = restoredBand;
                if (statusText) statusText.textContent = 'SSID Öffentlich';
            }
            
            fetch('/api/toggle_ap_visibility', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({visible: visible})
            }).then(response => response.json())
              .then(data => {
                  if (data.success) {
                      showToast(`SSID ${visible ? 'sichtbar' : 'versteckt'} gemacht!`, 'success');
                      setTimeout(() => location.reload(), 2000);
                  } else {
                      showToast('SSID-Sichtbarkeit konnte nicht geändert werden', 'error');
                  }
              });
        }
        
        function toggleWlan0Internet() {
            const enabled = document.getElementById('wlan0_internet_toggle').checked;
            
            fetch('/api/toggle_wlan0_internet', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({enabled: enabled})
            }).then(response => response.json())
              .then(data => {
                  if (data.success) {
                      showToast(`wlan0 Internet-Eingang ${enabled ? 'aktiviert' : 'deaktiviert'}!`, 'success');
                  } else {
                      showToast('wlan0 Internet-Konfiguration fehlgeschlagen', 'error');
                  }
              });
        }
        
        function updateEth0Mode() {
            const mode = document.getElementById('eth0_mode').value;
            
            fetch('/api/update_eth0_mode', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({mode: mode})
            }).then(response => response.json())
              .then(data => {
                  if (data.success) {
                      const modeText = mode === 'receive' ? 'Internet-Empfang' : 'Internet-Ausgabe';
                      showToast(`RJ45 auf ${modeText} umgestellt!`, 'success');
                      setTimeout(() => location.reload(), 2000);
                  } else {
                      showToast('RJ45 Modus-Update fehlgeschlagen', 'error');
                  }
              });
        }

        function togglePiHole() {
            const enabled = document.getElementById('pihole_toggle').checked;
            const action = enabled ? 'enable' : 'disable';
            
            fetch('/api/toggle_pihole', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({action: action})
            }).then(response => response.json())
              .then(data => {
                  if (data.success) {
                      showToast(`Pi-hole ${enabled ? 'aktiviert' : 'deaktiviert'}!`, 'success');
                      setTimeout(() => location.reload(), 2000);
                  } else {
                      showToast('Pi-hole Toggle fehlgeschlagen', 'error');
                  }
              });
        }
        
        function openPiHoleAdmin() {
            // Show password prompt
            Swal.fire({
                title: '🔐 Pi-hole Admin',
                input: 'password',
                inputLabel: 'Passwort eingeben',
                inputPlaceholder: 'Pi-hole Passwort',
                showCancelButton: true,
                confirmButtonText: 'Öffnen',
                cancelButtonText: 'Abbrechen'
            }).then((result) => {
                if (result.isConfirmed && result.value) {
                    fetch('/api/verify_pihole_password', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({password: result.value})
                    })
                    .then(response => response.json())
                    .then(data => {
                        if (data.success) {
                            window.open('http://192.168.50.1/admin', '_blank');
                        } else {
                            showToast('Falsches Passwort', 'error');
                        }
                    });
                }
            });
        }
        
        function cleanupOldClients() {
            Swal.fire({
                title: 'Alte Clients entfernen?',
                text: 'Nicht mehr aktive DHCP-Leases werden gelöscht.',
                icon: 'warning',
                showCancelButton: true,
                confirmButtonColor: '#ff9800',
                cancelButtonColor: '#718096',
                confirmButtonText: 'Ja, aufräumen!',
                cancelButtonText: 'Abbrechen'
            }).then((result) => {
                if (result.isConfirmed) {
                    fetch('/api/cleanup_clients', {method: 'POST'})
                        .then(response => response.json())
                        .then(data => {
                            if (data.success) {
                                showToast(`${data.removed} alte Clients entfernt!`, 'success');
                                setTimeout(() => location.reload(), 1500);
                            } else {
                                showToast('Fehler beim Aufräumen', 'error');
                            }
                        });
                }
            });
        }

        function restartService(service) {
            closeSystemModal();
            Swal.fire({
                title: `${service} neu starten?`,
                text: 'Der Service wird kurz unterbrochen.',
                icon: 'question',
                showCancelButton: true,
                confirmButtonColor: '#667eea',
                cancelButtonColor: '#718096',
                confirmButtonText: 'Ja, neu starten!',
                cancelButtonText: 'Abbrechen'
            }).then((result) => {
                if (result.isConfirmed) {
                    fetch('/api/restart_service', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({service: service})
                    }).then(response => response.json())
                      .then(data => {
                          showToast(`${service} neu gestartet!`, 'success');
                          setTimeout(() => location.reload(), 2000);
                      });
                }
            });
        }

        function openSystemModal() {
            document.getElementById('systemModal').style.display = 'block';
        }
        
        function closeSystemModal() {
            document.getElementById('systemModal').style.display = 'none';
        }
        
        // Theme Manager Functions
        function openThemeModal() {
            document.getElementById('themeModal').style.display = 'flex';
            loadThemes();
        }
        
        function closeThemeModal() {
            document.getElementById('themeModal').style.display = 'none';
        }
        
        async function loadThemes() {
            try {
                const response = await fetch('/api/themes/list');
                const themes = await response.json();
                
                const grid = document.getElementById('themesGrid');
                if (themes.length === 0) {
                    grid.innerHTML = `
                        <div style="text-align: center; padding: 40px; color: #999; grid-column: 1/-1;">
                            <i class="fas fa-palette" style="font-size: 48px; margin-bottom: 15px; opacity: 0.5;"></i>
                            <p>Keine Themes gefunden. Lade dein erstes Theme hoch!</p>
                        </div>
                    `;
                    return;
                }
                
                grid.innerHTML = themes.map(theme => `
                    <div class="theme-card ${theme.is_active ? 'active' : ''}" onclick="activateTheme('${theme.name}')">
                        ${theme.is_active ? '<div class="theme-badge">Aktiv</div>' : ''}
                        <div class="theme-card-image">
                            ${theme.has_screenshot ? 
                                `<img src="/api/themes/screenshot/${theme.name}" style="width:100%;height:100%;object-fit:cover;">` : 
                                `<i class="fas fa-palette"></i>`
                            }
                        </div>
                        <div class="theme-card-body">
                            <div class="theme-card-title">
                                ${theme.display_name}
                                ${theme.name === 'default' ? '<i class="fas fa-star" style="color:#f59e0b;"></i>' : ''}
                            </div>
                            <div class="theme-card-meta">
                                ${theme.description}<br>
                                <small>Version ${theme.version} • ${theme.author}</small>
                            </div>
                            ${theme.name !== 'default' ? `
                            <div class="theme-card-actions" onclick="event.stopPropagation();">
                                <button class="btn btn-danger btn-sm" onclick="deleteTheme('${theme.name}')">
                                    <i class="fas fa-trash"></i>
                                </button>
                            </div>
                            ` : ''}
                        </div>
                    </div>
                `).join('');
            } catch (error) {
                console.error('Error loading themes:', error);
                showToast('Fehler beim Laden der Themes', 'error');
            }
        }
        
        async function activateTheme(themeName) {
            try {
                const result = await Swal.fire({
                    title: 'Theme aktivieren?',
                    text: `Möchtest du das Theme "${themeName}" aktivieren? Das Dashboard wird neu geladen.`,
                    icon: 'question',
                    showCancelButton: true,
                    confirmButtonText: 'Ja, aktivieren',
                    cancelButtonText: 'Abbrechen'
                });
                
                if (result.isConfirmed) {
                    const response = await fetch('/api/themes/activate', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({theme_name: themeName})
                    });
                    
                    const data = await response.json();
                    
                    if (data.success) {
                        showToast('Theme aktiviert! Lade neu...', 'success');
                        setTimeout(() => location.reload(), 1500);
                    } else {
                        showToast(data.error || 'Fehler beim Aktivieren', 'error');
                    }
                }
            } catch (error) {
                console.error('Error activating theme:', error);
                showToast('Fehler beim Aktivieren des Themes', 'error');
            }
        }
        
        async function exportCurrentTheme() {
            try {
                showToast('Exportiere Theme...', 'info');
                
                const response = await fetch('/api/themes/export');
                if (!response.ok) throw new Error('Export failed');
                
                const blob = await response.blob();
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `openpirouter_theme_${new Date().toISOString().split('T')[0]}.zip`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                window.URL.revokeObjectURL(url);
                
                showToast('Theme erfolgreich exportiert!', 'success');
            } catch (error) {
                console.error('Error exporting theme:', error);
                showToast('Fehler beim Exportieren des Themes', 'error');
            }
        }
        
        async function uploadTheme(input) {
            if (!input.files || !input.files[0]) return;
            
            const file = input.files[0];
            if (!file.name.endsWith('.zip')) {
                showToast('Bitte wähle eine ZIP-Datei aus', 'error');
                return;
            }
            
            try {
                showToast('Lade Theme hoch...', 'info');
                
                const formData = new FormData();
                formData.append('theme', file);
                
                const response = await fetch('/api/themes/upload', {
                    method: 'POST',
                    body: formData
                });
                
                const data = await response.json();
                
                if (data.success) {
                    showToast('Theme erfolgreich hochgeladen!', 'success');
                    loadThemes();
                } else {
                    showToast(data.error || 'Fehler beim Hochladen', 'error');
                }
            } catch (error) {
                console.error('Error uploading theme:', error);
                showToast('Fehler beim Hochladen des Themes', 'error');
            } finally {
                input.value = '';
            }
        }
        
        async function deleteTheme(themeName) {
            try {
                const result = await Swal.fire({
                    title: 'Theme löschen?',
                    text: `Möchtest du das Theme "${themeName}" wirklich löschen? Dies kann nicht rückgängig gemacht werden.`,
                    icon: 'warning',
                    showCancelButton: true,
                    confirmButtonColor: '#e53e3e',
                    confirmButtonText: 'Ja, löschen',
                    cancelButtonText: 'Abbrechen'
                });
                
                if (result.isConfirmed) {
                    const response = await fetch('/api/themes/delete', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({theme_name: themeName})
                    });
                    
                    const data = await response.json();
                    
                    if (data.success) {
                        showToast('Theme gelöscht', 'success');
                        loadThemes();
                    } else {
                        showToast(data.error || 'Fehler beim Löschen', 'error');
                    }
                }
            } catch (error) {
                console.error('Error deleting theme:', error);
                showToast('Fehler beim Löschen des Themes', 'error');
            }
        }
        
        // Close modal when clicking outside
        window.onclick = function(event) {
            const systemModal = document.getElementById('systemModal');
            const themeModal = document.getElementById('themeModal');
            if (event.target == systemModal) {
                closeSystemModal();
            }
            if (event.target == themeModal) {
                closeThemeModal();
            }
        }
        
        function rebootSystem() {
            closeSystemModal();
            Swal.fire({
                title: 'System Neustart?',
                text: 'Alle Verbindungen werden getrennt!',
                icon: 'warning',
                showCancelButton: true,
                confirmButtonColor: '#f44336',
                cancelButtonColor: '#667eea',
                confirmButtonText: 'Ja, neu starten!',
                cancelButtonText: 'Abbrechen'
            }).then((result) => {
                if (result.isConfirmed) {
                    fetch('/api/reboot', {method: 'POST'})
                        .then(response => response.json())
                        .then(data => {
                            showToast('System wird neu gestartet...', 'success');
                        });
                }
            });
        }

        function exportConfig() {
            fetch('/api/export_config')
                .then(response => response.blob())
                .then(blob => {
                    const url = window.URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = 'pi-repeater-config.yaml';
                    a.click();
                });
        }

        function importConfig() {
            const file = document.getElementById('config_file').files[0];
            if (file) {
                const formData = new FormData();
                formData.append('file', file);
                
                fetch('/api/import_config', {
                    method: 'POST',
                    body: formData
                }).then(response => response.json())
                  .then(data => {
                      if (data.success) {
                          showToast('Konfiguration importiert!', 'success');
                          setTimeout(() => location.reload(), 2000);
                      } else {
                          showToast('Import fehlgeschlagen', 'error');
                      }
                  });
            }
        }

        function showToast(message, type = 'info') {
            const color = type === 'success' ? '#4CAF50' : type === 'error' ? '#f44336' : '#2196F3';
            
            Swal.fire({
                toast: true,
                position: 'top-end',
                icon: type,
                title: message,
                showConfirmButton: false,
                timer: 4000,
                timerProgressBar: true,
                background: color,
                color: 'white',
                iconColor: 'white'
            });
        }

        // Load internet speed
        
        // Initialize dashboard with WebSocket
        console.log('🚀 OpenPiRouter Dashboard initialized');
        
        // WebSocket handles all real-time updates
        // No additional polling needed
        
        // Legacy function kept for compatibility (not used)
        function loadInitialData() {
            // Load WiFi status
            fetch('/api/get_current_wifi')
                .then(response => response.json())
                .then(data => {
                    if (data.success && data.connected) {
                        document.getElementById('current_ssid').value = data.ssid;
                        document.getElementById('signal-value').textContent = data.signal + '%';
                        document.getElementById('current-wifi-section').style.display = 'block';
                        
                        const quality = getSignalQuality(data.signal);
                        const statusElement = document.getElementById('signal-status-text');
                        statusElement.textContent = quality.text;
                        statusElement.className = quality.class;
                    } else {
                        document.getElementById('current-wifi-section').style.display = 'none';
                        const statusElement = document.getElementById('signal-status-text');
                        statusElement.textContent = 'Keine WLAN-Verbindung';
                        statusElement.className = 'signal-none';
                    }
                })
                .catch(error => console.error('Error loading WiFi data:', error));
            
            // Load system status
            fetch('/api/status')
                .then(response => response.json())
                .then(data => {
                    updateSystemStatus(data);
                })
                .catch(error => console.error('Error loading status:', error));
            
            // Load system stats
            fetch('/api/stats')
                .then(response => response.json())
                .then(data => {
                    updateSystemStats(data);
                })
                .catch(error => console.error('Error loading stats:', error));
            
            // Load speed data
            fetch('/api/get_internet_speed')
                .then(response => response.json())
                .then(data => {
                    updateSpeedData(data);
                })
                .catch(error => console.error('Error loading speed data:', error));
        }
    </script>
</body>
</html>"""

def sh(cmd, timeout=10):
    """Execute shell command with timeout"""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, shell=True)
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return ""
    except Exception:
        return ""

@cached_function
def get_system_status():
    """Get system status information"""
    status = {
        'wifi': False,
        'internet': False,
        'ap': False,
        'pihole': False,
        'uptime': sh("uptime -p") or "Unbekannt"
    }
    
    # Check WiFi connection - check if wlan0 is connected
    try:
        # First check active connections (more reliable)
        result = subprocess.run(["nmcli", "-t", "-f", "name,device,state", "con", "show", "--active"], 
                              capture_output=True, timeout=15)
        if result.returncode == 0:
            for line in result.stdout.decode().splitlines():
                if "wlan0" in line and ("activated" in line or "connected" in line):
                    status['wifi'] = True
                    break
        
        # Fallback: check device status
        if not status['wifi']:
            result = subprocess.run(["nmcli", "-t", "-f", "device,state", "dev", "status"], 
                                  capture_output=True, timeout=10)
            if result.returncode == 0:
                for line in result.stdout.decode().splitlines():
                    if line.startswith("wlan0:") and ("connected" in line or "activated" in line):
                        status['wifi'] = True
                        break
    except:
        pass
    
    # Check internet
    try:
        result = subprocess.run(["ping", "-c", "1", "-W", "2", "1.1.1.1"], 
                              capture_output=True, timeout=5)
        status['internet'] = result.returncode == 0
    except:
        pass
    
    # Check Access Point
    try:
        result = subprocess.run(["systemctl", "is-active", "hostapd"], 
                              capture_output=True, timeout=5)
        status['ap'] = result.stdout.decode().strip() == "active"
    except:
        pass
    
    # Check Pi-hole
    try:
        result = subprocess.run(["systemctl", "is-active", "pihole-FTL"], 
                              capture_output=True, timeout=5)
        status['pihole'] = result.stdout.decode().strip() == "active"
    except:
        pass
    
    return status

@cached_function
def get_system_stats():
    """Get system statistics"""
    stats = {
        'cpu': 0,
        'memory': 0,
        'temperature': 0,
        'clients': 0,
        'disk_used': 0,
        'disk_free': 0,
        'disk_total': 0,
        'pihole_queries': 0,
        'pihole_blocked': 0,
        'pihole_blocked_percent': 0
    }
    
    try:
        # CPU usage
        stats['cpu'] = round(psutil.cpu_percent(interval=1))
        
        # Memory usage
        memory = psutil.virtual_memory()
        stats['memory'] = round(memory.percent)
        
        # Temperature (Raspberry Pi)
        try:
            temp_str = sh("vcgencmd measure_temp")
            temp_match = re.search(r'temp=([\d.]+)', temp_str)
            if temp_match:
                stats['temperature'] = round(float(temp_match.group(1)))
        except:
            pass
        
        # Disk usage (SD card)
        try:
            disk = psutil.disk_usage('/')
            stats['disk_total'] = round(disk.total / (1024**3), 1)  # GB
            stats['disk_used'] = round(disk.used / (1024**3), 1)    # GB
            stats['disk_free'] = round(disk.free / (1024**3), 1)    # GB
        except:
            stats['disk_total'] = 0
            stats['disk_used'] = 0
            stats['disk_free'] = 0
        
        # Connected clients
        try:
            stations = sh("iw dev wlan1 station dump")
            stats['clients'] = len([line for line in stations.splitlines() 
                                  if line.startswith("Station ")])
        except:
            pass
        
        # Pi-hole statistics (using FTL database counters)
        try:
            # Get Pi-hole stats from FTL database counters table
            import sqlite3
            db_path = '/etc/pihole/pihole-FTL.db'
            if os.path.exists(db_path):
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                
                # Get stats from counters table (ID 0 = total queries, ID 1 = blocked)
                cursor.execute("SELECT id, value FROM counters WHERE id IN (0, 1)")
                results = cursor.fetchall()
                
                total_queries = 0
                blocked_queries = 0
                
                for row in results:
                    if row[0] == 0:  # Total queries
                        total_queries = row[1]
                    elif row[0] == 1:  # Blocked queries
                        blocked_queries = row[1]
                
                stats['pihole_queries'] = total_queries
                stats['pihole_blocked'] = blocked_queries
                if total_queries > 0:
                    stats['pihole_blocked_percent'] = round((blocked_queries / total_queries) * 100, 1)
                else:
                    stats['pihole_blocked_percent'] = 0
                
                conn.close()
            else:
                # Fallback: set default values
                stats['pihole_queries'] = 0
                stats['pihole_blocked'] = 0
                stats['pihole_blocked_percent'] = 0
        except:
            # Fallback: set default values
            stats['pihole_queries'] = 0
            stats['pihole_blocked'] = 0
            stats['pihole_blocked_percent'] = 0
            
    except Exception:
        pass
    
    return stats

def get_internet_config():
    """Get current internet configuration"""
    config = {
        'wlan0_internet_enabled': True,   # wlan0 = Internet-Empfang (default)
        'eth0_mode': 'output'             # eth0 = Internet-Ausgabe (default, wie wlan1)
    }
    
    try:
        # Read wlan0 internet config
        if os.path.exists('/etc/pi-config/wlan0-internet-enabled'):
            with open('/etc/pi-config/wlan0-internet-enabled', 'r') as f:
                config['wlan0_internet_enabled'] = f.read().strip() == '1'
        
        # Read eth0 mode config  
        if os.path.exists('/etc/pi-config/eth0-mode'):
            with open('/etc/pi-config/eth0-mode', 'r') as f:
                config['eth0_mode'] = f.read().strip()
                
    except:
        pass
    
    return config

def get_wan_info():
    """Get WAN connection information"""
    info = {
        'ssid': 'Nicht verbunden',
        'signal': '-',
        'bitrate': '-',
        'ip': '-',
        'ping': 'Fail'
    }
    
    try:
        # Get active connection
        wifi_status = sh("nmcli -t -f active,ssid con show --active")
        for line in wifi_status.splitlines():
            if line.startswith("yes:"):
                info['ssid'] = line.split(":", 1)[1] or "Versteckt"
                break
        
        # Get signal strength and bitrate
        link_info = sh("iw dev wlan0 link")
        for line in link_info.splitlines():
            if "signal:" in line:
                info['signal'] = line.split("signal:")[-1].strip()
            elif "tx bitrate:" in line:
                info['bitrate'] = line.split("tx bitrate:")[-1].strip()
        
        # Get IP address
        ip_info = sh("ip addr show wlan0 | grep 'inet '")
        if ip_info:
            ip_match = re.search(r'inet (\d+\.\d+\.\d+\.\d+)', ip_info)
            if ip_match:
                info['ip'] = ip_match.group(1)
        
        # Test internet
        result = subprocess.run(["ping", "-c", "1", "-W", "2", "1.1.1.1"], 
                              capture_output=True, timeout=5)
        info['ping'] = "OK" if result.returncode == 0 else "Fail"
        
    except Exception:
        pass
    
    return info

def get_ap_info():
    """Get Access Point information"""
    info = {
        'ssid': 'Unbekannt',
        'band': '5G',
        'channel': '?',
        'password': '******',  # Default masked password
        'ssid_visible': True,  # Default to visible
        'clients': []
    }
    
    try:
        # Get AP password from hostapd config
        try:
            if os.path.exists(HOSTAPD):
                with open(HOSTAPD, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith('wpa_passphrase='):
                            ap_password = line.split('=', 1)[1].strip('"')
                            # Show actual password length as stars
                            info['password'] = '*' * len(ap_password)
                            break
        except:
            info['password'] = '******'
        
        # Parse hostapd config
        if os.path.exists(HOSTAPD):
            with open(HOSTAPD, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('ssid='):
                        info['ssid'] = line.split('=', 1)[1]
                    elif line.startswith('hw_mode='):
                        info['band'] = '5G' if line.split('=', 1)[1] == 'a' else '2G'
                    elif line.startswith('channel='):
                        info['channel'] = line.split('=', 1)[1]
                    elif line.startswith('ignore_broadcast_ssid='):
                        # 0 = visible, 1 = hidden, 2 = hidden (don't respond to broadcast probe requests)
                        visibility = line.split('=', 1)[1]
                        info['ssid_visible'] = visibility == '0'
        
        # Get connected clients from DHCP leases (most reliable)
        clients = []
        dhcp_clients = {}
        
        # Read DHCP leases
        try:
            with open('/var/lib/misc/dnsmasq.leases', 'r') as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 4:
                        # Format: timestamp MAC IP hostname client-id
                        mac = parts[1]
                        ip = parts[2]
                        hostname = parts[3] if parts[3] != '*' else ''
                        dhcp_clients[mac] = {'ip': ip, 'hostname': hostname}
        except:
            pass
        
        # Create client list from DHCP leases
        for mac, data in dhcp_clients.items():
            clients.append({
                'mac': mac,
                'ip': data['ip'],
                'hostname': data['hostname'],
                'signal': '',
                'interface': 'wlan1'
            })
        
        # Enrich with WiFi signal strength from wlan1
        try:
            stations = sh("iw dev wlan1 station dump")
            current_mac = None
            current_signal = ''
            
            for line in stations.splitlines():
                line = line.strip()
                if line.startswith("Station "):
                    current_mac = line.split()[1]
                    current_signal = ''
                elif line.startswith("signal:"):
                    current_signal = line.split("signal:")[-1].strip()
                    # Find matching client and update signal
                    for client in clients:
                        if client['mac'].lower() == current_mac.lower():
                            client['signal'] = current_signal
                            break
        except:
            pass
        
        info['clients'] = clients
        
    except Exception:
        pass
    
    return info

def get_dhcp_leases():
    """Get DHCP lease information"""
    leases = []
    try:
        if os.path.exists(LEASES):
            with open(LEASES, 'r') as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 4:
                        leases.append({
                            'exp': parts[0],
                            'mac': parts[1],
                            'ip': parts[2],
                            'hostname': parts[3] if parts[3] != '*' else ''
                        })
    except Exception:
        pass
    return leases

def get_pihole_info():
    """Get Pi-hole information"""
    try:
        with urllib.request.urlopen("http://127.0.0.1/admin/api.php?summary", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None

def load_config():
    """Load configuration"""
    try:
        if os.path.exists(CONF_FILE):
            with open(CONF_FILE, 'r') as f:
                return yaml.safe_load(f) or {}
    except Exception:
        pass
    return {}

def save_config(config):
    """Save configuration"""
    try:
        with open(CONF_FILE, 'w') as f:
            yaml.safe_dump(config, f)
        return True
    except Exception:
        return False

# Login template
LOGIN_TEMPLATE = '''
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OpenPiRouter Login</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            margin: 0;
            padding: 0;
            height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
        }
        .login-container {
            background: white;
            padding: 40px;
            border-radius: 20px;
            box-shadow: 0 20px 40px rgba(0,0,0,0.1);
            text-align: center;
            max-width: 400px;
            width: 90%;
        }
        .logo {
            font-size: 3em;
            margin-bottom: 20px;
        }
        h1 {
            color: #333;
            margin-bottom: 30px;
            font-weight: 300;
        }
        .form-group {
            margin-bottom: 20px;
            text-align: left;
        }
        label {
            display: block;
            margin-bottom: 8px;
            color: #555;
            font-weight: 500;
        }
        input[type="password"] {
            width: 100%;
            padding: 15px;
            border: 2px solid #e1e5e9;
            border-radius: 10px;
            font-size: 16px;
            transition: border-color 0.3s;
            box-sizing: border-box;
        }
        input[type="password"]:focus {
            outline: none;
            border-color: #667eea;
        }
        .btn {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 15px 30px;
            border-radius: 10px;
            font-size: 16px;
            cursor: pointer;
            transition: transform 0.2s;
            width: 100%;
        }
        .btn:hover {
            transform: translateY(-2px);
        }
        .error {
            color: #e53e3e;
            margin-top: 10px;
            font-size: 14px;
        }
    </style>
</head>
<body>
    <div class="login-container">
        <div class="logo">🚀</div>
        <h1>OpenPiRouter Dashboard</h1>
        <form method="POST" action="/login">
            <div class="form-group">
                <label for="password">🔐 Passwort:</label>
                <input type="password" id="password" name="password" required autofocus>
            </div>
            <button type="submit" class="btn">Anmelden</button>
        </form>
        {% if error %}
        <div class="error">{{ error }}</div>
        {% endif %}
    </div>
</body>
</html>
'''

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page"""
    if request.method == 'POST':
        password = request.form.get('password')
        if password == DASHBOARD_PASSWORD:
            session['authenticated'] = True
            return redirect('/')
        else:
            return render_template_string(LOGIN_TEMPLATE, error='❌ Falsches Passwort!')
    
    # Check if already authenticated
    if session.get('authenticated'):
        return redirect('/')
    
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/logout')
def logout():
    """Logout"""
    session.pop('authenticated', None)
    return redirect('/login')

@app.route('/')
def dashboard():
    """Main dashboard"""
    # Check authentication
    if not session.get('authenticated'):
        return redirect('/login')
    system_status = get_system_status()
    system_stats = get_system_stats()
    wan_info = get_wan_info()
    ap_info = get_ap_info()
    internet_config = get_internet_config()
    pihole_info = get_pihole_info()
    config = load_config()
    
    # Available channels based on band
    band = config.get('ap_band', ap_info.get('band', '5G'))
    available_channels = ["1", "6", "11"] if band == "2G" else ["36", "40", "44", "48"]
    
    return render_template_string(DASHBOARD_TEMPLATE,
        system_status=system_status,
        system_stats=system_stats,
        wan=wan_info,
        ap=ap_info,
        **internet_config,
        pihole=pihole_info,
        available_channels=available_channels,
        clients=ap_info.get('clients', []),
        scan_results=None,
        messages=[])


    try:
        scan_output = sh("nmcli -t -f ssid,signal,frequency,security dev wifi list")
        seen = {}
        
        for line in scan_output.splitlines():
            parts = line.split(":")
            if len(parts) >= 4:
                ssid, signal, freq, sec = parts[0], parts[1] or "0", parts[2], parts[3]
                key = (ssid, freq, sec)
                
                if key not in seen or int(signal) > int(seen[key]["signal"]):
                    seen[key] = {
                        "ssid": ssid,
                        "signal": signal,
                        "frequency": freq,
                        "security": sec
                    }
        
        scan_results = list(seen.values())
        
    except Exception:
        pass
    
    system_status = get_system_status()
    system_stats = get_system_stats()
    wan_info = get_wan_info()
    ap_info = get_ap_info()
    internet_config = get_internet_config()
    pihole_info = get_pihole_info()
    config = load_config()
    
    band = config.get('ap_band', ap_info.get('band', '5G'))
    available_channels = ["1", "6", "11"] if band == "2G" else ["36", "40", "44", "48"]
    
    return render_template_string(DASHBOARD_TEMPLATE,
        system_status=system_status,
        system_stats=system_stats,
        wan=wan_info,
        ap=ap_info,
        **internet_config,
        pihole=pihole_info,
        available_channels=available_channels,
        clients=ap_info.get('clients', []),
        scan_results=scan_results,
        messages=[])

# API Routes
@app.route('/api/disconnect_wifi', methods=['POST'])
def api_disconnect_wifi():
    """API: Disconnect from WiFi"""
    try:
        # Get current connection
        result = sh("nmcli -t -f name,device con show --active | grep wlan0 | cut -d: -f1", timeout=10)
        connection_name = result.strip()
        
        if connection_name:
            sh(f'nmcli con down "{connection_name}"', timeout=10)
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Keine aktive Verbindung'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/connect_wifi', methods=['POST'])
def api_connect_wifi():
    """API: Connect to WiFi"""
    try:
        data = request.get_json()
        ssid = data.get('ssid', '')
        password = data.get('password', '')
        
        if not ssid:
            return jsonify({'success': False, 'error': 'SSID erforderlich'})
        
        # Connect to WiFi
        cmd = f'nmcli dev wifi connect "{ssid}"'
        if password:
            cmd += f' password "{password}"'
        cmd += ' ifname wlan0'
        
        result = sh(cmd, timeout=30)
        
        # Save to config
        config = load_config()
        config['wan_ssid'] = ssid
        config['wan_pass'] = password
        save_config(config)
        
        return jsonify({'success': True})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/scan_wifi', methods=['GET'])
def api_scan_wifi():
    """API: Scan for WiFi networks"""
    try:
        # Force rescan
        result = subprocess.run(["nmcli", "dev", "wifi", "rescan"], 
                              capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': result.stderr})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/get_current_wifi', methods=['GET'])
def api_get_current_wifi():
    """API: Get current WiFi connection info with signal strength"""
    try:
        # Get connection info
        result = subprocess.run(["nmcli", "-t", "-f", "name,device,state", "con", "show", "--active"], 
                              capture_output=True, text=True, timeout=15)
        
        if result.returncode != 0:
            return jsonify({'success': False, 'error': result.stderr})
        
        for line in result.stdout.splitlines():
            if "wlan0" in line:
                parts = line.split(":")
                if len(parts) >= 3:
                    name = parts[0]
                    device = parts[1]
                    state = parts[2]
                    if device == "wlan0" and ("activated" in state or "connected" in state):
                        # Get signal strength from wifi list
                        signal_result = subprocess.run(["nmcli", "-t", "-f", "ssid,signal", "dev", "wifi", "list", "ifname", "wlan0"], 
                                                     capture_output=True, text=True, timeout=10)
                        signal = "0"
                        if signal_result.returncode == 0:
                            for line in signal_result.stdout.splitlines():
                                if name in line:
                                    parts = line.split(":")
                                    if len(parts) >= 2:
                                        signal = parts[1].strip()
                                        break
                        
                        return jsonify({'success': True, 'connected': True, 'ssid': name, 'signal': signal})
        
        return jsonify({'success': True, 'connected': False})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/get_internet_speed', methods=['GET'])
def api_get_internet_speed():
    """API: Get internet speed (download/upload)"""
    try:
        # Get network interface statistics
        result = subprocess.run(["cat", "/proc/net/dev"], capture_output=True, text=True, timeout=5)
        
        if result.returncode != 0:
            return jsonify({'success': False, 'error': result.stderr})
        
        lines = result.stdout.splitlines()
        wlan0_stats = None
        
        for line in lines:
            if "wlan0" in line:
                parts = line.split()
                if len(parts) >= 10:
                    # Format: interface | bytes | packets | errs | drop | fifo | frame | compressed | multicast | bytes | packets | errs | drop | fifo | colls | carrier | compressed
                    rx_bytes = int(parts[1])  # Received bytes
                    tx_bytes = int(parts[9])  # Transmitted bytes
                    wlan0_stats = {'rx_bytes': rx_bytes, 'tx_bytes': tx_bytes}
                break
        
        if not wlan0_stats:
            return jsonify({'success': False, 'error': 'wlan0 interface not found'})
        
        # Calculate speed (bytes per second over last 5 seconds)
        # This is a simple approximation - in production you'd want to store previous values
        # For now, we'll return the current byte counts and let frontend calculate rates
        return jsonify({
            'success': True, 
            'rx_bytes': wlan0_stats['rx_bytes'],
            'tx_bytes': wlan0_stats['tx_bytes'],
            'timestamp': int(time.time() * 1000)  # milliseconds
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/status')
def api_status():
    """API: Get system status"""
    return jsonify(get_system_status())

@app.route('/api/stats')
def api_stats():
    """API: Get system statistics"""
    return jsonify(get_system_stats())

@app.route('/api/get_wifi_networks', methods=['GET'])
def api_get_wifi_networks():
    """API: Get available WiFi networks"""
    try:
        # Use direct subprocess call instead of sh() function
        # Only scan with wlan0 to avoid duplicates from wlan1 (AP)
        result = subprocess.run(["nmcli", "-t", "-f", "ssid,signal,freq,security", "dev", "wifi", "list", "ifname", "wlan0"], 
                              capture_output=True, text=True, timeout=30)
        
        if result.returncode != 0:
            return jsonify({'success': False, 'error': result.stderr})
        
        scan_output = result.stdout
        networks = []
        seen = {}
        
        for line in scan_output.splitlines():
            if not line.strip():
                continue
            parts = line.split(":")
            if len(parts) >= 4:
                ssid, signal, freq, sec = parts[0], parts[1] or "0", parts[2], parts[3]
                # Skip if SSID is empty or is our own AP
                if not ssid.strip() or ssid.strip() == "PiRepeater":
                    continue
                # Group by SSID only, keep the best signal strength
                key = ssid.strip()
                
                if key not in seen or int(signal) > int(seen[key]["signal"]):
                    seen[key] = {
                        "ssid": ssid.strip(),
                        "signal": signal.strip(),
                        "frequency": freq.strip(),
                        "security": sec.strip() if sec.strip() else "Offen"
                    }
        
        networks = list(seen.values())
        return jsonify({'success': True, 'networks': networks})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/get_ap_info')
def api_get_ap_info():
    """API: Get Access Point information"""
    try:
        info = get_ap_info()
        return jsonify(info)
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/api/update_ap', methods=['POST'])
def api_update_ap():
    """API: Update Access Point settings"""
    try:
        data = request.get_json()
        
        # Update hostapd config
        updates = []
        if 'ssid' in data:
            updates.append(('ssid', data['ssid']))
        if 'password' in data and data['password'] and not data['password'].startswith('*'):
            # Only update password if it's not masked (starts with *)
            updates.append(('wpa_passphrase', data['password']))
        if 'band' in data:
            hw_mode = 'a' if data['band'] == '5G' else 'g'
            updates.append(('hw_mode', hw_mode))
        if 'channel' in data:
            updates.append(('channel', data['channel']))
        
        for key, value in updates:
            sh(f'sed -i "s/^{key}=.*/{key}={value}/" {HOSTAPD}')
        
        # Restart hostapd
        sh("systemctl restart hostapd")
        
        # Save to config
        config = load_config()
        config.update({f'ap_{k}': v for k, v in data.items()})
        save_config(config)
        
        return jsonify({'success': True})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/restart_ap', methods=['POST'])
def api_restart_ap():
    """API: Restart Access Point"""
    try:
        sh("systemctl restart hostapd")
        return jsonify({'success': True})
    except Exception:
        return jsonify({'success': False})

@app.route('/api/toggle_ap_visibility', methods=['POST'])
def api_toggle_ap_visibility():
    """API: Toggle Access Point SSID visibility"""
    try:
        data = request.get_json()
        visible = data.get('visible', True)
        
        # Read current hostapd config
        config_lines = []
        if os.path.exists(HOSTAPD):
            with open(HOSTAPD, 'r') as f:
                config_lines = f.readlines()
        
        # Update or add ignore_broadcast_ssid setting
        updated = False
        for i, line in enumerate(config_lines):
            if line.startswith('ignore_broadcast_ssid='):
                config_lines[i] = f'ignore_broadcast_ssid={"0" if visible else "1"}\n'
                updated = True
                break
        
        # If not found, add it
        if not updated:
            config_lines.append(f'ignore_broadcast_ssid={"0" if visible else "1"}\n')
        
        # For hidden SSIDs, switch to 2.4GHz for better compatibility
        # Many devices have problems with hidden SSIDs on 5GHz
        if not visible:
            # Switch to 2.4GHz (hw_mode=g, channel 6)
            for i, line in enumerate(config_lines):
                if line.startswith('hw_mode='):
                    config_lines[i] = 'hw_mode=g\n'
                elif line.startswith('channel='):
                    config_lines[i] = 'channel=6\n'
                elif line.startswith('ieee80211w='):
                    config_lines[i] = 'ieee80211w=0\n'
            
            # Add ieee80211w if not present
            if not any(line.startswith('ieee80211w=') for line in config_lines):
                config_lines.append('ieee80211w=0\n')
        else:
            # For visible SSIDs, switch back to 5GHz (hw_mode=a, channel 36)
            for i, line in enumerate(config_lines):
                if line.startswith('hw_mode='):
                    config_lines[i] = 'hw_mode=a\n'
                elif line.startswith('channel='):
                    config_lines[i] = 'channel=36\n'
            
            # Remove ieee80211w setting
            config_lines = [line for line in config_lines if not line.startswith('ieee80211w=')]
        
        # Write back to file
        with open(HOSTAPD, 'w') as f:
            f.writelines(config_lines)
        
        # Restart hostapd
        sh("systemctl restart hostapd")
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/toggle_wlan0_internet', methods=['POST'])
def api_toggle_wlan0_internet():
    """API: Toggle wlan0 Internet-Eingang"""
    try:
        data = request.get_json()
        enabled = data.get('enabled', True)
        
        # Save config
        with open('/etc/pi-config/wlan0-internet-enabled', 'w') as f:
            f.write('1' if enabled else '0')
        
        if enabled:
            # Enable wlan0 internet routing
            sh("iptables -t nat -A POSTROUTING -o wlan0 -j MASQUERADE")
            # Check if bridge exists and route accordingly
            bridge_result = subprocess.run(["ip", "link", "show", "br0"], capture_output=True, text=True, timeout=5)
            if bridge_result.returncode == 0:
                # Bridge exists, route bridge to wlan0
                sh("iptables -A FORWARD -i br0 -o wlan0 -j ACCEPT")
                sh("iptables -A FORWARD -i wlan0 -o br0 -j ACCEPT")
            else:
                # No bridge, route wlan1 to wlan0
                sh("iptables -A FORWARD -i wlan1 -o wlan0 -j ACCEPT")
                sh("iptables -A FORWARD -i wlan0 -o wlan1 -j ACCEPT")
        else:
            # Disable wlan0 internet routing
            try:
                sh("iptables -t nat -D POSTROUTING -o wlan0 -j MASQUERADE")
                sh("iptables -D FORWARD -i br0 -o wlan0 -j ACCEPT")
                sh("iptables -D FORWARD -i wlan0 -o br0 -j ACCEPT")
                sh("iptables -D FORWARD -i wlan1 -o wlan0 -j ACCEPT")
                sh("iptables -D FORWARD -i wlan0 -o wlan1 -j ACCEPT")
            except:
                pass
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/update_eth0_mode', methods=['POST'])
def api_update_eth0_mode():
    """API: Update eth0 mode (receive or output)"""
    try:
        data = request.get_json()
        mode = data.get('mode', 'output')
        
        # Save config
        with open('/etc/pi-config/eth0-mode', 'w') as f:
            f.write(mode)
        
        if mode == 'receive':
            # eth0 = Internet-Empfang (wie wlan0)
            # Remove bridge and restore original wlan1 configuration
            try:
                sh("ip link set br0 down")
                sh("ip link delete br0")
            except:
                pass
            
            # Restore wlan1 original configuration
            sh("ip addr add 192.168.50.1/24 dev wlan1")
            sh("ip link set wlan1 up")
            
            # Configure eth0 as WAN interface (no static IP, DHCP)
            sh("ip addr flush dev eth0")
            sh("ip link set eth0 up")
            
            # Clean up bridge-related iptables rules
            try:
                sh("iptables -t nat -D POSTROUTING -o br0 -j MASQUERADE")
                sh("iptables -D FORWARD -i br0 -o wlan0 -j ACCEPT")
                sh("iptables -D FORWARD -i wlan0 -o br0 -j ACCEPT")
            except:
                pass
            
            # Restore original dnsmasq configuration
            with open('/etc/dnsmasq.d/pi-repeater.conf', 'w') as f:
                f.write('''interface=wlan1
bind-interfaces
dhcp-range=192.168.50.10,192.168.50.200,12h
dhcp-option=3,192.168.50.1
dhcp-option=6,192.168.50.1
port=0
domain=lan
expand-hosts
''')
            sh("systemctl restart dnsmasq")
            
        elif mode == 'output':
            # eth0 = Internet-Ausgabe (wie wlan1)
            # Create bridge br0 and add both wlan1 and eth0 to it
            # First create bridge if it doesn't exist
            try:
                sh("ip link add name br0 type bridge")
            except:
                pass
            sh("ip link set br0 up")
            sh("ip addr add 192.168.50.1/24 dev br0")
            
            # Add wlan1 to bridge
            sh("ip addr flush dev wlan1")
            try:
                sh("brctl addif br0 wlan1")
            except:
                pass
            sh("ip link set wlan1 up")
            
            # Add eth0 to bridge
            sh("ip addr flush dev eth0")
            try:
                sh("brctl addif br0 eth0")
            except:
                pass
            sh("ip link set eth0 up")
            
            # Configure routing for bridge
            sh("iptables -t nat -A POSTROUTING -o br0 -j MASQUERADE")
            sh("iptables -A FORWARD -i br0 -o wlan0 -j ACCEPT")
            sh("iptables -A FORWARD -i wlan0 -o br0 -j ACCEPT")
            
            # Update dnsmasq to use bridge instead of wlan1
            with open('/etc/dnsmasq.d/pi-repeater.conf', 'w') as f:
                f.write('''interface=br0
bind-interfaces
dhcp-range=192.168.50.10,192.168.50.200,12h
dhcp-option=3,192.168.50.1
dhcp-option=6,192.168.50.1
port=0
domain=lan
expand-hosts
''')
            sh("systemctl restart dnsmasq")
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/verify_pihole_password', methods=['POST'])
def api_verify_pihole_password():
    """API: Verify Pi-hole admin password"""
    try:
        data = request.get_json()
        password = data.get('password', '')
        
        if password == PIHOLE_PASSWORD:
            return jsonify({'success': True})
        else:
            return jsonify({'success': False})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/toggle_pihole', methods=['POST'])
def api_toggle_pihole():
    """API: Toggle Pi-hole"""
    try:
        data = request.get_json()
        action = data.get('action', '')
        
        if action == 'enable':
            sh("pihole enable")
        elif action == 'disable':
            sh("pihole disable")
        
        return jsonify({'success': True})
    except Exception:
        return jsonify({'success': False})

@app.route('/api/cleanup_clients', methods=['POST'])
def api_cleanup_clients():
    """API: Remove old/expired DHCP leases"""
    try:
        import time
        current_time = int(time.time())
        removed_count = 0
        active_leases = []
        
        # Read current leases
        lease_file = '/var/lib/misc/dnsmasq.leases'
        if os.path.exists(lease_file):
            with open(lease_file, 'r') as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 4:
                        lease_time = int(parts[0])
                        # Keep only leases that haven't expired (future timestamp)
                        if lease_time > current_time:
                            active_leases.append(line)
                        else:
                            removed_count += 1
            
            # Write back only active leases
            with open(lease_file, 'w') as f:
                f.writelines(active_leases)
            
            # Restart dnsmasq to reload leases
            sh("systemctl restart dnsmasq")
        
        return jsonify({'success': True, 'removed': removed_count})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/restart_service', methods=['POST'])
def api_restart_service():
    """API: Restart service"""
    try:
        data = request.get_json()
        service = data.get('service', '')
        
        if service in ['hostapd', 'dnsmasq', 'pihole-FTL']:
            sh(f"systemctl restart {service}")
        
        return jsonify({'success': True})
    except Exception:
        return jsonify({'success': False})

@app.route('/api/reboot', methods=['POST'])
def api_reboot():
    """API: Reboot system"""
    try:
        threading.Thread(target=lambda: sh("reboot", timeout=1)).start()
        return jsonify({'success': True})
    except Exception:
        return jsonify({'success': False})

@app.route('/api/export_config')
def api_export_config():
    """API: Export complete configuration"""
    try:
        # Read AP settings from hostapd.conf
        ap_ssid = "PiRepeater"
        ap_pass = "BitteAendern123"
        ap_band = "5G"
        ap_channel = "36"
        ap_visible = True
        
        try:
            with open('/etc/hostapd/hostapd.conf', 'r') as f:
                hostapd_conf = f.read()
                
                # Extract SSID
                ssid_match = re.search(r'ssid=(.+)', hostapd_conf)
                if ssid_match:
                    ap_ssid = ssid_match.group(1).strip()
                
                # Extract password
                pass_match = re.search(r'wpa_passphrase=(.+)', hostapd_conf)
                if pass_match:
                    ap_pass = pass_match.group(1).strip()
                
                # Extract band (hw_mode)
                band_match = re.search(r'hw_mode=(.+)', hostapd_conf)
                if band_match:
                    hw_mode = band_match.group(1).strip()
                    ap_band = "5G" if hw_mode == "a" else "2.4G"
                
                # Extract channel
                channel_match = re.search(r'channel=(.+)', hostapd_conf)
                if channel_match:
                    ap_channel = channel_match.group(1).strip()
                
                # Check if SSID is hidden
                if 'ignore_broadcast_ssid=1' in hostapd_conf:
                    ap_visible = False
        except Exception as e:
            print(f"Error reading hostapd.conf: {e}")
        
        # Get WAN SSID
        wan_ssid = "Nicht verbunden"
        try:
            result = sh("nmcli -t -f name,device connection show --active")
            for line in result.split('\n'):
                if 'wlan0' in line:
                    wan_ssid = line.split(':')[0]
                    break
        except:
            pass
        
        # Get DHCP range from dnsmasq
        dhcp_range = "192.168.50.100,192.168.50.200"
        try:
            with open('/etc/dnsmasq.d/pi-repeater.conf', 'r') as f:
                for line in f:
                    if line.startswith('dhcp-range='):
                        dhcp_range = line.split('=')[1].split(',')[0] + ',' + line.split(',')[1]
                        break
        except:
            pass
        
        # Get Internet configuration (wlan0/eth0)
        wlan0_internet_enabled = True  # Default to true
        eth0_mode = "receive"
        
        try:
            if os.path.exists('/etc/pi-config/wlan0-internet-enabled'):
                with open('/etc/pi-config/wlan0-internet-enabled', 'r') as f:
                    content = f.read().strip()
                    # Handle both 'true'/'false' and '1'/'0' formats
                    wlan0_internet_enabled = content in ['true', '1']
        except:
            pass
        
        try:
            if os.path.exists('/etc/pi-config/eth0-mode'):
                with open('/etc/pi-config/eth0-mode', 'r') as f:
                    eth0_mode = f.read().strip()
        except:
            pass
        
        # Get all current settings
        config = {
            # AP Settings (wlan1)
            'wlan1_ap_ssid': ap_ssid,
            'wlan1_ap_pass': ap_pass,
            'wlan1_ap_band': ap_band,
            'wlan1_ap_channel': ap_channel,
            'wlan1_ap_visible': ap_visible,
            
            # WAN Settings (wlan0)
            'wlan0_wan_ssid': wan_ssid,
            'wlan0_wan_pass': "",  # For security, don't export passwords
            
            # Internet Configuration
            'wlan0_internet_enabled': wlan0_internet_enabled,
            'eth0_mode': eth0_mode,
            
            # Pi-hole Settings
            'pihole_enabled': sh("systemctl is-active pihole-FTL") == "active",
            'pihole_password': PIHOLE_PASSWORD,  # Pi-hole admin password
            
            # System Settings
            'web_port': WEB_PORT,
            'hostname': sh("hostname"),
            'timezone': sh("timedatectl show --property=Timezone --value"),
            'dashboard_password': DASHBOARD_PASSWORD,  # Dashboard login password
            
            # Network Settings
            'ap_ip': "192.168.50.1",
            'ap_subnet': "192.168.50.0/24",
            'dhcp_range': dhcp_range,
            
            # Services Status
            'hostapd_enabled': sh("systemctl is-enabled hostapd") == "enabled",
            'dnsmasq_enabled': sh("systemctl is-enabled dnsmasq") == "enabled",
            'network_manager_enabled': sh("systemctl is-enabled NetworkManager") == "enabled",
            
            # Export Info
            'export_timestamp': datetime.now().isoformat(),
            'export_version': "2.0",
            'pi_repeater_version': "modern_dashboard"
        }
        
        data = yaml.safe_dump(config, default_flow_style=False, sort_keys=False).encode()
        return send_file(io.BytesIO(data), as_attachment=True, 
                        download_name="pi-repeater-config.yaml", 
                        mimetype="text/yaml")
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/import_config', methods=['POST'])
def api_import_config():
    """API: Import complete configuration"""
    try:
        file = request.files.get('file')
        if not file:
            return jsonify({'success': False, 'error': 'Keine Datei hochgeladen'})
            
        config = yaml.safe_load(file.read().decode())
        if not isinstance(config, dict):
            return jsonify({'success': False, 'error': 'Ungültige Konfigurationsdatei'})
        
        # ===== AP Settings (hostapd.conf) =====
        hostapd_updates = []
        
        # Support both old and new field names
        ap_ssid = config.get('wlan1_ap_ssid') or config.get('ap_ssid')
        ap_pass = config.get('wlan1_ap_pass') or config.get('ap_pass')
        ap_band = config.get('wlan1_ap_band') or config.get('ap_band')
        ap_channel = config.get('wlan1_ap_channel') or config.get('ap_channel')
        ap_visible = config.get('wlan1_ap_visible')
        if ap_visible is None:
            ap_visible = config.get('ap_visible')
        
        if ap_ssid:
            hostapd_updates.append(('ssid', ap_ssid))
            
        if ap_pass:
            hostapd_updates.append(('wpa_passphrase', ap_pass))
        
        if ap_band:
            hw_mode = 'a' if ap_band == '5G' else 'g'
            hostapd_updates.append(('hw_mode', hw_mode))
        
        if ap_channel:
            hostapd_updates.append(('channel', str(ap_channel)))
        
        if ap_visible is not None:
            visibility = '0' if ap_visible else '1'
            hostapd_updates.append(('ignore_broadcast_ssid', visibility))
        
        # Apply hostapd updates
        for key, value in hostapd_updates:
            sh(f'sed -i "s/^{key}=.*/{key}={value}/" /etc/hostapd/hostapd.conf')
        
        # ===== Internet Configuration =====
        if 'wlan0_internet_enabled' in config:
            os.makedirs('/etc/pi-config', exist_ok=True)
            with open('/etc/pi-config/wlan0-internet-enabled', 'w') as f:
                f.write('true' if config['wlan0_internet_enabled'] else 'false')
        
        if 'eth0_mode' in config:
            os.makedirs('/etc/pi-config', exist_ok=True)
            with open('/etc/pi-config/eth0-mode', 'w') as f:
                f.write(config['eth0_mode'])
        
        # ===== DHCP Settings (dnsmasq) =====
        if 'dhcp_range' in config and config['dhcp_range']:
            # Update dnsmasq dhcp-range
            dhcp_parts = config['dhcp_range'].split(',')
            if len(dhcp_parts) >= 2:
                sh(f"sed -i 's/^dhcp-range=.*/dhcp-range={dhcp_parts[0]},{dhcp_parts[1]},24h/' /etc/dnsmasq.d/pi-repeater.conf")
        
        # ===== System Settings =====
        if 'hostname' in config and config['hostname']:
            sh(f"hostnamectl set-hostname '{config['hostname']}'")
            
        if 'timezone' in config and config['timezone']:
            sh(f"timedatectl set-timezone '{config['timezone']}'")
            
        if 'web_port' in config and config['web_port']:
            sh(f"sed -i 's/Environment=WEB_PORT=.*/Environment=WEB_PORT={config['web_port']}/' /etc/systemd/system/pi-config.service")
            sh("systemctl daemon-reload")
        
        # ===== Dashboard Password =====
        # Note: This requires code modification to support dynamic passwords
        # For now, password remains hardcoded in the Python script
            
        # ===== Service Settings =====
        if 'hostapd_enabled' in config:
            if config['hostapd_enabled']:
                sh("systemctl enable hostapd")
            else:
                sh("systemctl disable hostapd")
                
        if 'dnsmasq_enabled' in config:
            if config['dnsmasq_enabled']:
                sh("systemctl enable dnsmasq")
            else:
                sh("systemctl disable dnsmasq")
        
        if 'pihole_enabled' in config:
            if config['pihole_enabled']:
                sh("systemctl enable pihole-FTL")
                sh("systemctl start pihole-FTL")
            else:
                sh("systemctl disable pihole-FTL")
                sh("systemctl stop pihole-FTL")
                
        # ===== Restart affected services =====
        sh("systemctl restart hostapd")
        sh("systemctl restart dnsmasq")
        sh("systemctl restart NetworkManager")
        
        return jsonify({'success': True, 'message': 'Konfiguration erfolgreich importiert. System wird in 5 Sekunden neu gestartet...'})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

# Theme Manager API Routes
@app.route('/api/themes/list')
def api_themes_list():
    """API: List all available themes"""
    try:
        if not theme_manager:
            return jsonify([])
        
        themes = theme_manager.list_themes()
        return jsonify(themes)
    except Exception as e:
        print(f"Error listing themes: {e}")
        return jsonify([])

@app.route('/api/themes/activate', methods=['POST'])
def api_themes_activate():
    """API: Activate a theme"""
    try:
        if not theme_manager:
            return jsonify({'success': False, 'error': 'Theme manager not available'})
        
        data = request.get_json()
        theme_name = data.get('theme_name')
        
        if not theme_name:
            return jsonify({'success': False, 'error': 'Theme name required'})
        
        theme_manager.activate_theme(theme_name)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/themes/export')
def api_themes_export():
    """API: Export current theme"""
    try:
        if not theme_manager:
            return jsonify({'success': False, 'error': 'Theme manager not available'}), 400
        
        # Get current dashboard HTML
        current_template = DASHBOARD_TEMPLATE
        
        # Get active theme name
        active_theme = theme_manager.get_active_theme()
        
        # Export theme
        zip_data = theme_manager.export_theme(active_theme, current_template)
        
        # Send as download
        return send_file(
            io.BytesIO(zip_data),
            mimetype='application/zip',
            as_attachment=True,
            download_name=f'openpirouter_{active_theme}_{datetime.now().strftime("%Y%m%d")}.zip'
        )
    except Exception as e:
        print(f"Error exporting theme: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/themes/upload', methods=['POST'])
def api_themes_upload():
    """API: Upload a new theme"""
    try:
        if not theme_manager:
            return jsonify({'success': False, 'error': 'Theme manager not available'})
        
        if 'theme' not in request.files:
            return jsonify({'success': False, 'error': 'No file uploaded'})
        
        file = request.files['theme']
        
        if file.filename == '':
            return jsonify({'success': False, 'error': 'No file selected'})
        
        if not file.filename.endswith('.zip'):
            return jsonify({'success': False, 'error': 'File must be a ZIP archive'})
        
        # Read file data
        zip_data = file.read()
        
        # Upload theme
        theme_name = theme_manager.upload_theme(zip_data)
        
        return jsonify({'success': True, 'theme_name': theme_name})
    except Exception as e:
        print(f"Error uploading theme: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/themes/delete', methods=['POST'])
def api_themes_delete():
    """API: Delete a theme"""
    try:
        if not theme_manager:
            return jsonify({'success': False, 'error': 'Theme manager not available'})
        
        data = request.get_json()
        theme_name = data.get('theme_name')
        
        if not theme_name:
            return jsonify({'success': False, 'error': 'Theme name required'})
        
        theme_manager.delete_theme(theme_name)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/themes/screenshot/<theme_name>')
def api_themes_screenshot(theme_name):
    """API: Get theme screenshot"""
    try:
        if not theme_manager:
            return '', 404
        
        screenshot_path = os.path.join('/opt/pi-config/themes', theme_name, 'screenshot.png')
        
        if os.path.exists(screenshot_path):
            return send_file(screenshot_path, mimetype='image/png')
        else:
            return '', 404
    except Exception as e:
        print(f"Error serving screenshot: {e}")
        return '', 404

if __name__ == "__main__":
    # Initialize theme system
    if theme_manager:
        try:
            theme_manager.ensure_themes_dir()
            # Save current template as default theme
            default_template_path = '/opt/pi-config/themes/default/template.html'
            os.makedirs(os.path.dirname(default_template_path), exist_ok=True)
            with open(default_template_path, 'w', encoding='utf-8') as f:
                f.write(DASHBOARD_TEMPLATE)
            # Create default meta.json
            default_meta = {
                'name': 'default',
                'display_name': 'OpenPiRouter Default',
                'description': 'Standard OpenPiRouter Dashboard Theme',
                'author': 'OpenPiRouter',
                'version': '1.0'
            }
            with open('/opt/pi-config/themes/default/meta.json', 'w') as f:
                json.dump(default_meta, f, indent=2)
            print("Theme system initialized")
        except Exception as e:
            print(f"Warning: Could not initialize theme system: {e}")
    
    # Start background update thread
    update_thread = threading.Thread(target=background_update_task, daemon=True)
    update_thread.start()
    print("Background update thread started")
    
    print(f"Starting OpenPiRouter Dashboard with WebSocket support on port {WEB_PORT}")
    socketio.run(app, host="0.0.0.0", port=WEB_PORT, debug=False, allow_unsafe_werkzeug=True)
