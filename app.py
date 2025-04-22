from flask import Flask, request, jsonify, render_template, Response
from flask_cors import CORS
import requests
import xml.etree.ElementTree as ET
from urllib.parse import urljoin
from datetime import datetime
import time, json, os, csv
from io import StringIO

app = Flask(__name__)
CORS(app)

HISTORY_FILE = 'crawl_history.json'

if not os.path.exists(HISTORY_FILE):
    with open(HISTORY_FILE, 'w') as f:
        json.dump([], f)

def fetch_xml(url):
    try:
        res = requests.get(url, timeout=10, allow_redirects=True)
        res.raise_for_status()
        return res.content, res.url if res.history else None
    except Exception as e:
        print(f"Lỗi khi tải sitemap {url}:", e)
        return None, None

def parse_recursive(xml_data, depth=0):
    urls = []
    try:
        root = ET.fromstring(xml_data)
        ns = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        for loc in root.findall(".//ns:url/ns:loc", ns):
            urls.append(loc.text)

        for sitemap in root.findall(".//ns:sitemap/ns:loc", ns):
            child_url = sitemap.text
            print("→ Đang phân tích:", child_url)
            child_xml, _ = fetch_xml(child_url)
            if child_xml:
                urls.extend(parse_recursive(child_xml, depth + 1))
    except Exception as e:
        print("Lỗi khi phân tích XML:", e)
    return urls

def save_history(domain, url_count, duration):
    history = []
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, 'r') as f:
            history = json.load(f)

    history.insert(0, {
        "domain": domain,
        "url_count": url_count,
        "duration_sec": round(duration, 2),
        "timestamp": datetime.now().isoformat()
    })

    with open(HISTORY_FILE, 'w') as f:
        json.dump(history[:20], f)  # giữ 20 dòng gần nhất

def normalize_url(raw_url):
    if not raw_url.startswith("http"):
        raw_url = "https://" + raw_url
    if not raw_url.endswith(".xml"):
        if not raw_url.endswith("/"):
            raw_url += "/"
        raw_url += "sitemap.xml"
    return raw_url

@app.route('/')
def index():
    return render_template("index.html")

@app.route('/api/crawl', methods=['POST'])
def crawl():
    data = request.get_json()
    raw_urls = data.get("urls")
    include = data.get("include")
    exclude = data.get("exclude")

    if not raw_urls:
        return jsonify({"error": "Thiếu URL"}), 400

    all_results = []

    for base_url in raw_urls:
        base_url = normalize_url(base_url)

        start = time.time()

        xml_data, redirected_to = fetch_xml(base_url)
        if not xml_data:
            all_results.append({
                "domain": base_url,
                "error": "Không thể tải sitemap",
                "urls": []
            })
            continue

        urls = parse_recursive(xml_data)

        if include:
            urls = [u for u in urls if include in u]
        if exclude:
            urls = [u for u in urls if exclude not in u]

        duration = time.time() - start
        save_history(base_url, len(urls), duration)

        all_results.append({
            "domain": base_url,
            "redirected_to": redirected_to,
            "count": len(urls),
            "duration": round(duration, 2),
            "urls": urls
        })

    return jsonify(all_results)

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

@app.route('/api/check-index', methods=['POST'])
def check_index():
    data = request.get_json()
    urls = data.get("urls", [])
    results = []

    for url in urls:
        status = get_index_status(url)
        results.append({"url": url, "status": status})

    return jsonify(results)

def get_index_status(url):
    try:
        res = requests.get(url, timeout=10)
        if 'noindex' in res.headers.get('X-Robots-Tag', '').lower():
            return 'Noindex'
        if '<meta name="robots"' in res.text.lower() and 'noindex' in res.text.lower():
            return 'Noindex'
        return 'Index'
    except:
        return 'Lỗi'

if __name__ == '__main__':
    app.run(debug=True)