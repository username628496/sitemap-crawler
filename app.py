# app.py - Updated Version

from flask import Flask, request, jsonify, render_template, Response
from flask_cors import CORS
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
import time, json, os, csv
from io import StringIO

app = Flask(__name__)
CORS(app)

HISTORY_FILE = 'crawl_history.json'
if not os.path.exists(HISTORY_FILE):
    with open(HISTORY_FILE, 'w') as f:
        json.dump([], f)

def fetch_url(url):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.93 Safari/537.36'}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        return res.text
    except Exception as e:
        print(f"[Lỗi] Không truy cập được: {url}")
        return None

def discover_sitemaps(domain):
    sitemaps = []
    robots_url = f"https://{domain}/robots.txt"
    robots_txt = fetch_url(robots_url)
    if robots_txt:
        for line in robots_txt.splitlines():
            if line.lower().startswith('sitemap:'):
                sitemap_url = line.split(':', 1)[1].strip()
                sitemaps.append(sitemap_url)
    if not sitemaps:
        for path in ['sitemap.xml', 'sitemap_index.xml']:
            test_url = f"https://{domain}/{path}"
            if fetch_url(test_url):
                sitemaps.append(test_url)
    return sitemaps

def parse_sitemap(url):
    urls = []
    xml_data = fetch_url(url)
    if not xml_data:
        return urls
    try:
        root = ET.fromstring(xml_data)
        ns = {'ns': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        for loc in root.findall('.//ns:url/ns:loc', ns):
            urls.append(loc.text)
        for sitemap in root.findall('.//ns:sitemap/ns:loc', ns):
            urls.extend(parse_sitemap(sitemap.text))
    except Exception as e:
        print(f"[Lỗi XML] {url}")
    return urls

def save_history(domain, url_count, duration):
    with open(HISTORY_FILE, 'r') as f:
        history = json.load(f)
    history.insert(0, {
        "domain": domain,
        "url_count": url_count,
        "duration_sec": round(duration, 2),
        "timestamp": datetime.now().isoformat()
    })
    with open(HISTORY_FILE, 'w') as f:
        json.dump(history[:20], f)

@app.route('/')
def index():
    return render_template("index.html")

@app.route('/api/crawl', methods=['POST'])
def crawl():
    data = request.get_json()
    domains = data.get("domains", [])

    if not domains:
        return jsonify({"error": "Thiếu domain"}), 400

    results = []
    for domain in domains:
        domain_clean = domain.replace('https://','').replace('http://','').strip('/')
        sitemaps = discover_sitemaps(domain_clean)
        domain_result = {"domain": domain_clean, "sitemaps": []}
        for sitemap_url in sitemaps:
            start = time.time()
            urls = parse_sitemap(sitemap_url)
            duration = time.time() - start
            domain_result["sitemaps"].append({
                "sitemap": sitemap_url,
                "count": len(urls),
                "urls": list(set(urls)),
                "duration": round(duration, 2)
            })
        total_urls = sum(len(s["urls"]) for s in domain_result["sitemaps"])
        save_history(domain_clean, total_urls, duration)
        results.append(domain_result)

    return jsonify(results)

@app.route('/api/history')
def get_history():
    with open(HISTORY_FILE, 'r') as f:
        data = json.load(f)
    return jsonify(data)

@app.route('/api/export', methods=['POST'])
def export_urls():
    data = request.get_json()
    urls = data.get("urls", [])
    export_type = data.get("type", "csv")
    if export_type == "txt":
        return Response("\n".join(urls), mimetype="text/plain",
                        headers={"Content-Disposition": "attachment; filename=urls.txt"})
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["URL"])
    for u in urls:
        writer.writerow([u])
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=urls.csv"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=False)
