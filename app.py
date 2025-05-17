from flask import Flask, request, jsonify, render_template, Response
from flask_cors import CORS
import requests
import xml.etree.ElementTree as ET
from xml.etree.ElementTree import ParseError
from datetime import datetime
import time, json, os, csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import StringIO
import pytz

app = Flask(__name__)
CORS(app)

HISTORY_FILE = 'crawl_history.json'
if not os.path.exists(HISTORY_FILE):
    with open(HISTORY_FILE, 'w') as f:
        json.dump([], f)

def fetch_url(url):
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
        ),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    try:
        res = requests.get(url, headers=headers, timeout=10)
        res.raise_for_status()
        return res.text
    except requests.exceptions.HTTPError as e:
        if res.status_code == 403:
            raise Exception(f"403 Forbidden – Trang từ chối truy cập: {url}")
        raise Exception(f"Lỗi HTTP {res.status_code} – {url}")
    except requests.exceptions.RequestException as e:
        raise Exception(f"Không thể kết nối: {url} – {str(e)}")

def is_valid_xml(text):
    try:
        ET.fromstring(text)
        return True
    except ET.ParseError:
        return False

def discover_sitemaps(domain):
    sitemaps = []
    robots_url = f"https://{domain}/robots.txt"
    robots_txt = fetch_url(robots_url)
    if robots_txt:
        for line in robots_txt.splitlines():
            if line.lower().startswith('sitemap:'):
                sitemap_url = line.split(':', 1)[1].strip()
                content = fetch_url(sitemap_url)
                if content and is_valid_xml(content):
                    sitemaps.append(sitemap_url)
    if not sitemaps:
        for path in ['sitemap.xml', 'sitemap_index.xml']:
            url = f"https://{domain}/{path}"
            content = fetch_url(url)
            if content and is_valid_xml(content):
                sitemaps.append(url)
    return sitemaps

def parse_sitemap(url):
    urls = []
    xml_data = fetch_url(url)
    if not xml_data:
        raise Exception("Không tải được sitemap")
    try:
        root = ET.fromstring(xml_data)
        ns = {'ns': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        for loc in root.findall('.//ns:url/ns:loc', ns):
            urls.append(loc.text)
        for sitemap in root.findall('.//ns:sitemap/ns:loc', ns):
            urls.extend(parse_sitemap(sitemap.text.strip()))
    except ParseError as e:
        raise Exception(f"XML lỗi: {str(e)}")
    return urls

def save_history(domain, url_count, duration, status, urls):
    history = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r') as f:
                history = json.load(f)
        except json.JSONDecodeError:
            history = []

    tz = pytz.timezone("Asia/Ho_Chi_Minh")
    timestamp = datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')
    history.insert(0, {
        "domain": domain,
        "url_count": url_count,
        "duration_sec": round(duration, 2),
        "timestamp": timestamp,
        "status": status,
        "urls": urls[:200]
    })

    with open(HISTORY_FILE, 'w') as f:
        json.dump(history[:50], f, ensure_ascii=False, indent=2)

def process_domain(domain):
    domain_clean = domain.replace('https://','').replace('http://','').strip('/')
    result = {"domain": domain_clean, "sitemaps": [], "status": "success"}
    all_urls = []
    try:
        sitemaps = discover_sitemaps(domain_clean)
        if not sitemaps:
            raise Exception("Không tìm thấy sitemap")

        for sitemap_url in sitemaps:
            start = time.time()
            urls = parse_sitemap(sitemap_url)
            duration = time.time() - start
            urls = list(set(urls))
            all_urls.extend(urls)
            result["sitemaps"].append({
                "sitemap": sitemap_url,
                "count": len(urls),
                "urls": urls,
                "duration": round(duration, 2)
            })

        total_urls = len(set(all_urls))
        save_history(domain_clean, total_urls, duration, result["status"], list(set(all_urls)))

    except Exception as e:
        result["status"] = "failed"
        result["error"] = str(e)
        save_history(domain_clean, 0, 0, "failed", [])

    return result

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
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(process_domain, d) for d in domains]
        for future in as_completed(futures):
            results.append(future.result())
    return jsonify(results)

@app.route('/api/crawl-stream')
def crawl_stream():
    def stream(domains):
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(process_domain, d): d for d in domains}
            for future in as_completed(futures):
                try:
                    result = future.result()
                except Exception as e:
                    result = {
                        "domain": futures[future],
                        "status": "failed",
                        "error": str(e)
                    }
                yield f"data: {json.dumps(result)}\n\n"
    domains = request.args.get("domains", "")
    domain_list = domains.split(",") if domains else []
    return Response(stream(domain_list), content_type='text/event-stream')

@app.route('/api/history')
def get_history():
    try:
        with open(HISTORY_FILE, 'r') as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({"error": f"Lỗi đọc lịch sử: {str(e)}"}), 500

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