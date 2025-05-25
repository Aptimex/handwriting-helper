# Website Local Hosting

This is an offline copy of the calligrapher.ai website, taken from the [Web Archive](https://web.archive.org/web/20240924102434/https://www.calligrapher.ai/). 

Changes:
- Removed Google Analytics file and import
- Changed the `css.css` href to load from filesystem
- Saved the `d.bin` file that was remotely hosted by CloudFront
- Point the `d.bin` download URL to 127.0.0.1:8000 so it can be served locally (requires CORS)

## Getting Started

```
python3 ./server.py
```

Then open `Calligrapher.ai.htm` in your browser. 

