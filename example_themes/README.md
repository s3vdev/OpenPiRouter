# 🎨 OpenPiRouter Example Themes

This directory contains example themes that you can use as templates for creating your own custom OpenPiRouter dashboard themes.

## Available Themes

### Default Theme (`openpirouter_default_theme_with_screenshot.zip`)
The standard OpenPiRouter dashboard theme with purple gradient header and modern card-based layout.

**Features:**
- Clean, modern design
- Purple gradient color scheme
- Responsive card layout
- Real-time WebSocket updates
- Mobile-friendly

**Screenshot included:** Yes

## How to Use

### Installation
1. Open your OpenPiRouter Dashboard
2. Click the **Theme Manager** icon (🎨) in the header
3. Click **"Theme Hochladen"**
4. Select the ZIP file
5. Click on the theme card to activate it

### Customization
1. Download and extract the ZIP file
2. Edit `template.html`:
   - Change colors in the `<style>` section
   - Modify layout in the `<body>` section
   - Add custom JavaScript in the `<script>` section
3. Replace `screenshot.png` with your own preview image (recommended: 800x600px)
4. Edit `meta.json` to update theme information:
   ```json
   {
     "name": "my_custom_theme",
     "display_name": "My Custom Theme",
     "description": "My personalized OpenPiRouter dashboard",
     "author": "Your Name",
     "version": "1.0"
   }
   ```
5. Zip all files together
6. Upload via Theme Manager

## Theme Structure

Each theme ZIP must contain:

```
my_theme.zip
├── template.html    (Required) - Complete dashboard HTML/CSS/JS
├── meta.json       (Required) - Theme metadata
├── screenshot.png  (Optional) - Preview image
└── README.md       (Optional) - Theme documentation
```

### template.html
Complete, self-contained HTML file with embedded CSS and JavaScript. This is your entire dashboard.

### meta.json
```json
{
  "name": "unique_theme_name",           // Unique identifier (alphanumeric + _ -)
  "display_name": "Beautiful Theme",     // Display name in Theme Manager
  "description": "A beautiful theme",    // Short description
  "author": "Your Name",                 // Theme creator
  "version": "1.0"                       // Version number
}
```

### screenshot.png
Preview image shown in Theme Manager. Recommended dimensions: 800x600px or 16:9 aspect ratio.

## Theme Development Tips

### Color Schemes
Change the gradient background in the CSS:
```css
background: linear-gradient(135deg, #your-color-1 0%, #your-color-2 100%);
```

### Card Styles
Modify card appearance:
```css
.card {
    background: rgba(255,255,255,0.95);
    border-radius: 15px;
    /* Add your custom styles */
}
```

### Status Colors
Update status indicator colors:
```css
.status-online { color: #38a169; }    /* Green */
.status-offline { color: #e53e3e; }   /* Red */
.status-warning { color: #d69e2e; }   /* Orange */
```

### Button Styles
Customize button appearance:
```css
.btn {
    background: linear-gradient(135deg, #your-color 0%, #your-darker-color 100%);
    /* ... */
}
```

## Testing Your Theme

1. **Local Testing**: Edit the template.html directly and open in a browser (limited functionality)
2. **Live Testing**: Upload to OpenPiRouter and activate to see it with real data
3. **Export & Iterate**: Make changes, re-zip, and re-upload

## Sharing Your Theme

Created an awesome theme? Share it with the community:

1. Upload your theme ZIP to GitHub Gists or similar
2. Share the link in OpenPiRouter Discussions
3. Include screenshots and description

## Best Practices

✅ **Do:**
- Keep template.html self-contained (all CSS/JS embedded)
- Test on mobile devices
- Include clear screenshot
- Document custom features in README
- Use semantic HTML

❌ **Don't:**
- Don't include external dependencies that require internet
- Don't modify Flask/Python backend code in themes
- Don't include sensitive information
- Don't break WebSocket functionality

## Advanced: API Integration

Your theme has access to all dashboard APIs:

```javascript
// Get system status
fetch('/api/status').then(r => r.json()).then(console.log);

// Get connected clients
fetch('/api/get_ap_info').then(r => r.json()).then(console.log);

// WebSocket for real-time updates
socket.on('system_status', (data) => {
    // Update your custom UI
});
```

## Troubleshooting

**Theme doesn't upload:**
- Check ZIP structure (files must be in root, not in subfolder)
- Ensure template.html exists
- Verify meta.json is valid JSON

**Theme looks broken after activation:**
- Check browser console for JavaScript errors
- Verify all CSS is embedded in template.html
- Ensure WebSocket connection works

**Screenshot doesn't show:**
- File must be named `screenshot.png` (lowercase)
- Must be PNG format
- Recommended size: < 2MB

## Need Help?

- **Documentation**: See main README.md
- **Issues**: [GitHub Issues](https://github.com/s3vdev/OpenPiRouter/issues)
- **Discussions**: [GitHub Discussions](https://github.com/s3vdev/OpenPiRouter/discussions)

---

**Happy Theming! 🎨**

