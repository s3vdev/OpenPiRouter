#!/usr/bin/env python3
"""
OpenPiRouter Theme Manager
Handles theme uploads, exports, activation and management
"""
import os
import json
import shutil
import zipfile
import tempfile
from pathlib import Path
from datetime import datetime

THEMES_DIR = '/opt/pi-config/themes'
ACTIVE_THEME_LINK = os.path.join(THEMES_DIR, 'active_theme')
DEFAULT_THEME = 'default'

def ensure_themes_dir():
    """Ensure themes directory structure exists"""
    os.makedirs(THEMES_DIR, exist_ok=True)
    
    # Create default theme directory if it doesn't exist
    default_dir = os.path.join(THEMES_DIR, DEFAULT_THEME)
    if not os.path.exists(default_dir):
        os.makedirs(default_dir, exist_ok=True)

def get_active_theme():
    """Get the currently active theme name"""
    if os.path.islink(ACTIVE_THEME_LINK):
        return os.path.basename(os.readlink(ACTIVE_THEME_LINK))
    return DEFAULT_THEME

def list_themes():
    """List all available themes with metadata"""
    ensure_themes_dir()
    themes = []
    
    if not os.path.exists(THEMES_DIR):
        return themes
    
    for theme_name in os.listdir(THEMES_DIR):
        theme_path = os.path.join(THEMES_DIR, theme_name)
        
        # Skip if not a directory or if it's the symlink
        if not os.path.isdir(theme_path) or theme_name == 'active_theme':
            continue
        
        meta_file = os.path.join(theme_path, 'meta.json')
        screenshot_file = os.path.join(theme_path, 'screenshot.png')
        template_file = os.path.join(theme_path, 'template.html')
        
        # Check if theme is valid (has required files)
        if not os.path.exists(template_file):
            continue
        
        # Read metadata
        meta = {
            'name': theme_name,
            'display_name': theme_name.replace('_', ' ').title(),
            'description': 'Custom theme',
            'author': 'Unknown',
            'version': '1.0',
            'created': datetime.fromtimestamp(os.path.getctime(theme_path)).strftime('%Y-%m-%d'),
            'has_screenshot': os.path.exists(screenshot_file),
            'is_active': theme_name == get_active_theme()
        }
        
        if os.path.exists(meta_file):
            try:
                with open(meta_file, 'r') as f:
                    custom_meta = json.load(f)
                    meta.update(custom_meta)
            except:
                pass
        
        themes.append(meta)
    
    return sorted(themes, key=lambda x: (not x['is_active'], x['name']))

def activate_theme(theme_name):
    """Activate a specific theme"""
    ensure_themes_dir()
    
    theme_path = os.path.join(THEMES_DIR, theme_name)
    
    if not os.path.exists(theme_path):
        raise ValueError(f"Theme '{theme_name}' does not exist")
    
    template_file = os.path.join(theme_path, 'template.html')
    if not os.path.exists(template_file):
        raise ValueError(f"Theme '{theme_name}' is missing template.html")
    
    # Remove old symlink if exists
    if os.path.islink(ACTIVE_THEME_LINK):
        os.unlink(ACTIVE_THEME_LINK)
    elif os.path.exists(ACTIVE_THEME_LINK):
        shutil.rmtree(ACTIVE_THEME_LINK)
    
    # Create new symlink
    os.symlink(theme_path, ACTIVE_THEME_LINK)
    
    return True

def export_theme(theme_name, current_template_html):
    """Export a theme as a ZIP file"""
    ensure_themes_dir()
    
    theme_path = os.path.join(THEMES_DIR, theme_name)
    
    # Create temporary directory for export
    with tempfile.TemporaryDirectory() as temp_dir:
        export_path = os.path.join(temp_dir, f'{theme_name}.zip')
        
        with zipfile.ZipFile(export_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            # Add template.html (current dashboard HTML)
            zipf.writestr('template.html', current_template_html)
            
            # Add metadata
            meta = {
                'name': theme_name,
                'display_name': theme_name.replace('_', ' ').title(),
                'description': f'Exported theme from OpenPiRouter',
                'author': 'OpenPiRouter',
                'version': '1.0',
                'exported': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            zipf.writestr('meta.json', json.dumps(meta, indent=2))
            
            # Add screenshot if exists
            if os.path.exists(theme_path):
                screenshot = os.path.join(theme_path, 'screenshot.png')
                if os.path.exists(screenshot):
                    zipf.write(screenshot, 'screenshot.png')
            
            # Add README
            readme = """# OpenPiRouter Theme

## Installation
1. Upload this ZIP file via the Theme Manager in OpenPiRouter Dashboard
2. Click on the theme preview to activate it

## Structure
- template.html: Main dashboard template
- screenshot.png: Theme preview image
- meta.json: Theme metadata

## Customization
Edit template.html to customize the dashboard appearance.
All HTML, CSS, and JavaScript can be modified.
"""
            zipf.writestr('README.md', readme)
        
        # Read the ZIP file
        with open(export_path, 'rb') as f:
            return f.read()

def upload_theme(zip_file_bytes, theme_name=None):
    """Upload and install a new theme from ZIP file"""
    ensure_themes_dir()
    
    # Create temporary directory for extraction
    with tempfile.TemporaryDirectory() as temp_dir:
        zip_path = os.path.join(temp_dir, 'theme.zip')
        
        # Write uploaded file
        with open(zip_path, 'wb') as f:
            f.write(zip_file_bytes)
        
        # Extract ZIP
        extract_dir = os.path.join(temp_dir, 'extracted')
        with zipfile.ZipFile(zip_path, 'r') as zipf:
            zipf.extractall(extract_dir)
        
        # Validate theme structure
        template_file = os.path.join(extract_dir, 'template.html')
        if not os.path.exists(template_file):
            raise ValueError("Theme must contain template.html")
        
        # Read metadata
        meta_file = os.path.join(extract_dir, 'meta.json')
        if os.path.exists(meta_file):
            with open(meta_file, 'r') as f:
                meta = json.load(f)
                if not theme_name:
                    theme_name = meta.get('name', 'custom_theme')
        
        if not theme_name:
            theme_name = f'custom_theme_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
        
        # Sanitize theme name
        theme_name = ''.join(c if c.isalnum() or c in '_-' else '_' for c in theme_name)
        
        # Create theme directory
        theme_path = os.path.join(THEMES_DIR, theme_name)
        
        # If theme exists, backup old version
        if os.path.exists(theme_path):
            backup_path = f"{theme_path}_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            shutil.move(theme_path, backup_path)
        
        # Copy theme files
        shutil.copytree(extract_dir, theme_path)
        
        return theme_name

def delete_theme(theme_name):
    """Delete a theme"""
    if theme_name == DEFAULT_THEME:
        raise ValueError("Cannot delete default theme")
    
    if theme_name == get_active_theme():
        raise ValueError("Cannot delete active theme. Activate another theme first.")
    
    theme_path = os.path.join(THEMES_DIR, theme_name)
    
    if not os.path.exists(theme_path):
        raise ValueError(f"Theme '{theme_name}' does not exist")
    
    shutil.rmtree(theme_path)
    return True

def get_theme_template(theme_name=None):
    """Get the HTML template for a specific theme"""
    if not theme_name:
        theme_name = get_active_theme()
    
    theme_path = os.path.join(THEMES_DIR, theme_name)
    template_file = os.path.join(theme_path, 'template.html')
    
    if os.path.exists(template_file):
        with open(template_file, 'r', encoding='utf-8') as f:
            return f.read()
    
    return None

