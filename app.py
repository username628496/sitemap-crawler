from flask import Flask, request, jsonify, render_template, Response
from flask_cors import CORS
import requests
import xml.etree.ElementTree as ET
from xml.etree.ElementTree import ParseError
from datetime import datetime, timedelta
import time, json, os, csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import StringIO
from collections import defaultdict, Counter
from typing import Dict, List, Optional
import pytz
import sqlite3

# Flask app initialization
app = Flask(__name__)
CORS(app)

# Legacy JSON history file for migration
HISTORY_FILE = 'crawl_history.json'

# Enhanced History Manager
class CrawlHistoryManager:
    def __init__(self, db_path='crawl_history.db'):
        self.db_path = db_path
        self.init_database()
    
    def init_database(self):
        """Initialize SQLite database for better performance"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Main crawl sessions table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS crawl_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain TEXT NOT NULL,
                timestamp DATETIME NOT NULL,
                status TEXT NOT NULL,
                total_urls INTEGER DEFAULT 0,
                duration_sec REAL DEFAULT 0,
                sitemaps_found INTEGER DEFAULT 0,
                error_message TEXT,
                user_agent TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Detailed sitemap results table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sitemap_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER,
                sitemap_url TEXT NOT NULL,
                urls_found INTEGER DEFAULT 0,
                processing_time REAL DEFAULT 0,
                status TEXT DEFAULT 'success',
                error_message TEXT,
                FOREIGN KEY (session_id) REFERENCES crawl_sessions (id)
            )
        ''')
        
        # Sample URLs table (store only sample, not all URLs)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sample_urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER,
                url TEXT NOT NULL,
                url_type TEXT DEFAULT 'page',
                FOREIGN KEY (session_id) REFERENCES crawl_sessions (id)
            )
        ''')
        
        # Performance metrics table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS performance_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER,
                metric_name TEXT NOT NULL,
                metric_value REAL NOT NULL,
                metric_unit TEXT DEFAULT '',
                FOREIGN KEY (session_id) REFERENCES crawl_sessions (id)
            )
        ''')
        
        # Create indexes for better performance
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_domain ON crawl_sessions(domain)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_timestamp ON crawl_sessions(timestamp)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_status ON crawl_sessions(status)')
        
        conn.commit()
        conn.close()
    
    def migrate_from_json(self):
        """Migrate existing JSON history to SQLite"""
        if not os.path.exists(HISTORY_FILE):
            return 0
        
        try:
            with open(HISTORY_FILE, 'r') as f:
                old_history = json.load(f)
            
            migrated_count = 0
            for record in old_history:
                session_id = self.save_crawl_session(
                    domain=record.get('domain', ''),
                    status=record.get('status', 'unknown'),
                    total_urls=record.get('url_count', 0),
                    duration=record.get('duration_sec', 0),
                    sample_urls=record.get('urls', [])[:50],
                    sitemaps_data=None,
                    error_message=None,
                    timestamp_override=record.get('timestamp')
                )
                if session_id:
                    migrated_count += 1
            
            # Backup old file
            if migrated_count > 0:
                os.rename(HISTORY_FILE, f'{HISTORY_FILE}.backup')
                print(f"Migrated {migrated_count} records from JSON to SQLite")
            
            return migrated_count
        except Exception as e:
            print(f"Migration error: {e}")
            return 0
    
    def save_crawl_session(self, domain: str, status: str, 
                          total_urls: int = 0, duration: float = 0,
                          sitemaps_data: List[Dict] = None,
                          error_message: str = None,
                          sample_urls: List[str] = None,
                          timestamp_override: str = None):
        """Save enhanced crawl session"""
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            tz = pytz.timezone("Asia/Ho_Chi_Minh")
            if timestamp_override:
                # For migration - use existing timestamp
                timestamp = timestamp_override
            else:
                timestamp = datetime.now(tz).strftime('%Y-%m-%d %H:%M:%S')
            
            # Insert main session
            cursor.execute('''
                INSERT INTO crawl_sessions 
                (domain, timestamp, status, total_urls, duration_sec, 
                 sitemaps_found, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                domain, timestamp, status, total_urls, duration,
                len(sitemaps_data) if sitemaps_data else 0, error_message
            ))
            
            session_id = cursor.lastrowid
            
            # Insert sitemap details
            if sitemaps_data:
                for sitemap in sitemaps_data:
                    cursor.execute('''
                        INSERT INTO sitemap_results 
                        (session_id, sitemap_url, urls_found, processing_time, status, error_message)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (
                        session_id,
                        sitemap.get('sitemap', ''),
                        sitemap.get('count', 0),
                        sitemap.get('duration', 0),
                        'success' if 'error' not in sitemap else 'failed',
                        sitemap.get('error', None)
                    ))
            
            # Store sample URLs (max 50 per session)
            if sample_urls:
                sample_urls_limited = sample_urls[:50]
                for url in sample_urls_limited:
                    # Detect URL type
                    url_type = self.detect_url_type(url)
                    cursor.execute('''
                        INSERT INTO sample_urls (session_id, url, url_type)
                        VALUES (?, ?, ?)
                    ''', (session_id, url, url_type))
            
            # Store performance metrics
            if duration > 0 and total_urls > 0:
                urls_per_second = total_urls / duration
                cursor.execute('''
                    INSERT INTO performance_metrics 
                    (session_id, metric_name, metric_value, metric_unit)
                    VALUES (?, ?, ?, ?)
                ''', (session_id, 'urls_per_second', urls_per_second, 'urls/sec'))
            
            conn.commit()
            return session_id
            
        except Exception as e:
            conn.rollback()
            print(f"Error saving crawl session: {e}")
            return None
        finally:
            conn.close()
    
    def detect_url_type(self, url: str) -> str:
        """Detect URL type for categorization"""
        url_lower = url.lower()
        
        if any(ext in url_lower for ext in ['.pdf', '.doc', '.docx', '.xls', '.xlsx']):
            return 'document'
        elif any(ext in url_lower for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']):
            return 'image'  
        elif any(ext in url_lower for ext in ['.mp4', '.avi', '.mov', '.wmv']):
            return 'video'
        elif '/blog/' in url_lower or '/news/' in url_lower or '/article/' in url_lower:
            return 'blog'
        elif '/product/' in url_lower or '/shop/' in url_lower or '/store/' in url_lower:
            return 'product'
        else:
            return 'page'
    
    def get_history(self, limit: int = 20, offset: int = 0, 
                   domain_filter: str = None, status_filter: str = None,
                   date_from: str = None, date_to: str = None) -> Dict:
        """Get crawl history with advanced filtering"""
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Build dynamic query
        where_conditions = []
        params = []
        
        if domain_filter:
            where_conditions.append("domain LIKE ?")
            params.append(f"%{domain_filter}%")
        
        if status_filter:
            where_conditions.append("status = ?")
            params.append(status_filter)
        
        if date_from:
            where_conditions.append("DATE(timestamp) >= ?")
            params.append(date_from)
        
        if date_to:
            where_conditions.append("DATE(timestamp) <= ?")
            params.append(date_to)
        
        where_clause = "WHERE " + " AND ".join(where_conditions) if where_conditions else ""
        
        # Get total count
        count_query = f"SELECT COUNT(*) FROM crawl_sessions {where_clause}"
        cursor.execute(count_query, params)
        total_count = cursor.fetchone()[0]
        
        # Get paginated results
        main_query = f'''
            SELECT id, domain, timestamp, status, total_urls, duration_sec, 
                   sitemaps_found, error_message
            FROM crawl_sessions 
            {where_clause}
            ORDER BY timestamp DESC 
            LIMIT ? OFFSET ?
        '''
        
        cursor.execute(main_query, params + [limit, offset])
        sessions = cursor.fetchall()
        
        # Format results
        results = []
        for session in sessions:
            session_id, domain, timestamp, status, total_urls, duration, sitemaps_found, error_msg = session
            
            # Get sitemap details for this session
            cursor.execute('''
                SELECT sitemap_url, urls_found, processing_time, status, error_message
                FROM sitemap_results WHERE session_id = ?
            ''', (session_id,))
            sitemaps = cursor.fetchall()
            
            # Get sample URLs
            cursor.execute('''
                SELECT url, url_type FROM sample_urls 
                WHERE session_id = ? LIMIT 10
            ''', (session_id,))
            sample_urls = cursor.fetchall()
            
            results.append({
                "id": session_id,
                "domain": domain,
                "timestamp": timestamp,
                "status": status,
                "total_urls": total_urls,
                "duration_sec": duration,
                "sitemaps_found": sitemaps_found,
                "error_message": error_msg,
                "sitemaps": [
                    {
                        "url": sm[0],
                        "urls_found": sm[1], 
                        "processing_time": sm[2],
                        "status": sm[3],
                        "error": sm[4]
                    } for sm in sitemaps
                ],
                "sample_urls": [
                    {"url": url[0], "type": url[1]} for url in sample_urls
                ]
            })
        
        conn.close()
        
        return {
            "results": results,
            "total": total_count,
            "limit": limit,
            "offset": offset,
            "filters_applied": {
                "domain": domain_filter,
                "status": status_filter,
                "date_from": date_from,
                "date_to": date_to
            }
        }
    
    def get_statistics(self, days: int = 30) -> Dict:
        """Get comprehensive statistics"""
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        date_limit = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        
        stats = {}
        
        # Basic counts
        cursor.execute('''
            SELECT 
                COUNT(*) as total_crawls,
                SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as successful_crawls,
                SUM(total_urls) as total_urls_found,
                AVG(duration_sec) as avg_duration,
                AVG(total_urls) as avg_urls_per_crawl
            FROM crawl_sessions 
            WHERE DATE(timestamp) >= ?
        ''', (date_limit,))
        
        basic_stats = cursor.fetchone()
        stats['basic'] = {
            "total_crawls": basic_stats[0] or 0,
            "successful_crawls": basic_stats[1] or 0,
            "success_rate": round((basic_stats[1] / basic_stats[0] * 100) if basic_stats[0] > 0 else 0, 1),
            "total_urls_found": basic_stats[2] or 0,
            "avg_duration": round(basic_stats[3] or 0, 2),
            "avg_urls_per_crawl": round(basic_stats[4] or 0, 1)
        }
        
        # Top domains
        cursor.execute('''
            SELECT domain, COUNT(*) as crawl_count, SUM(total_urls) as total_urls
            FROM crawl_sessions 
            WHERE DATE(timestamp) >= ?
            GROUP BY domain 
            ORDER BY crawl_count DESC 
            LIMIT 10
        ''', (date_limit,))
        
        stats['top_domains'] = [
            {"domain": row[0], "crawl_count": row[1], "total_urls": row[2] or 0}
            for row in cursor.fetchall()
        ]
        
        # Daily activity
        cursor.execute('''
            SELECT DATE(timestamp) as date, 
                   COUNT(*) as crawls,
                   SUM(total_urls) as urls_found
            FROM crawl_sessions 
            WHERE DATE(timestamp) >= ?
            GROUP BY DATE(timestamp)
            ORDER BY date DESC
            LIMIT 30
        ''', (date_limit,))
        
        stats['daily_activity'] = [
            {"date": row[0], "crawls": row[1], "urls_found": row[2] or 0}
            for row in cursor.fetchall()
        ]
        
        # Error analysis
        cursor.execute('''
            SELECT error_message, COUNT(*) as count
            FROM crawl_sessions 
            WHERE status = 'failed' AND DATE(timestamp) >= ? AND error_message IS NOT NULL
            GROUP BY error_message
            ORDER BY count DESC
            LIMIT 10
        ''', (date_limit,))
        
        stats['common_errors'] = [
            {"error": row[0], "count": row[1]}
            for row in cursor.fetchall()
        ]
        
        conn.close()
        return stats
    
    def compare_crawls(self, domain: str, limit: int = 5) -> Dict:
        """Compare recent crawls for a domain"""
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT timestamp, total_urls, duration_sec, status
            FROM crawl_sessions 
            WHERE domain = ? 
            ORDER BY timestamp DESC 
            LIMIT ?
        ''', (domain, limit))
        
        crawls = cursor.fetchall()
        conn.close()
        
        if len(crawls) < 2:
            return {"message": "Cần ít nhất 2 lần crawl để so sánh"}
        
        comparison = {
            "domain": domain,
            "crawls": [],
            "trends": {}
        }
        
        for crawl in crawls:
            comparison["crawls"].append({
                "timestamp": crawl[0],
                "total_urls": crawl[1] or 0,
                "duration": crawl[2] or 0,
                "status": crawl[3]
            })
        
        # Calculate trends
        if len(crawls) >= 2:
            latest = crawls[0]
            previous = crawls[1]
            
            url_change = (latest[1] or 0) - (previous[1] or 0)
            duration_change = (latest[2] or 0) - (previous[2] or 0)
            
            comparison["trends"] = {
                "url_change": url_change,
                "url_change_percent": round((url_change / previous[1] * 100) if previous[1] and previous[1] > 0 else 0, 1),
                "duration_change": round(duration_change, 2),
                "duration_change_percent": round((duration_change / previous[2] * 100) if previous[2] and previous[2] > 0 else 0, 1)
            }
        
        return comparison

# Initialize global history manager and migrate existing data
history_manager = CrawlHistoryManager()
history_manager.migrate_from_json()

# Legacy functions (keep for backward compatibility)
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
    def try_fetch(domain_variant):
        found_sitemaps = []
        try:
            robots_url = f"https://{domain_variant}/robots.txt"
            robots_txt = fetch_url(robots_url)
            if robots_txt:
                for line in robots_txt.splitlines():
                    if line.lower().startswith('sitemap:'):
                        sitemap_url = line.split(':', 1)[1].strip()
                        content = fetch_url(sitemap_url)
                        if content and is_valid_xml(content):
                            found_sitemaps.append(sitemap_url)
        except Exception:
            pass

        if not found_sitemaps:
            for path in ['sitemap.xml', 'sitemap_index.xml']:
                try:
                    url = f"https://{domain_variant}/{path}"
                    content = fetch_url(url)
                    if content and is_valid_xml(content):
                        found_sitemaps.append(url)
                except Exception:
                    continue

        return found_sitemaps

    # Try domain as entered
    sitemaps = try_fetch(domain)

    # If not found and doesn't start with www, try with www
    if not sitemaps and not domain.startswith('www.'):
        www_domain = f"www.{domain}"
        sitemaps = try_fetch(www_domain)

    return sitemaps

def parse_sitemap(url, visited_sitemaps=None, max_depth=10):
    """Parse sitemap with recursion protection"""
    if visited_sitemaps is None:
        visited_sitemaps = set()
    
    # Prevent infinite recursion
    if url in visited_sitemaps or len(visited_sitemaps) >= max_depth:
        return []
    
    visited_sitemaps.add(url)
    urls = []
    
    xml_data = fetch_url(url)
    if not xml_data:
        raise Exception("Không tải được sitemap")
    
    try:
        root = ET.fromstring(xml_data)
        ns = {'ns': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        
        for loc in root.findall('.//ns:url/ns:loc', ns):
            link = loc.text.strip()
            # Filter out sitemap files
            if 'sitemap.xml' not in link.lower() and 'sitemap_index.xml' not in link.lower():
                urls.append(link)

        # Process nested sitemaps with recursion protection
        for sitemap in root.findall('.//ns:sitemap/ns:loc', ns):
            sitemap_url = sitemap.text.strip()
            try:
                nested_urls = parse_sitemap(sitemap_url, visited_sitemaps.copy(), max_depth)
                urls.extend(nested_urls)
            except Exception as e:
                print(f"Warning: Could not parse nested sitemap {sitemap_url}: {e}")
                continue
                
    except ParseError as e:
        raise Exception(f"XML lỗi: {str(e)}")
    
    return urls

def save_enhanced_history(domain, status, total_urls=0, duration=0, 
                         sitemaps_data=None, error_message=None, sample_urls=None):
    """Save crawl session with enhanced tracking"""
    
    return history_manager.save_crawl_session(
        domain=domain,
        status=status,
        total_urls=total_urls,
        duration=duration,
        sitemaps_data=sitemaps_data,
        error_message=error_message,
        sample_urls=sample_urls
    )

def process_domain(domain):
    """Enhanced domain processing with better history tracking"""
    
    start_time = time.time()
    sitemaps_data = []
    all_urls = set()
    
    try:
        domain_clean = domain.replace('https://', '').replace('http://', '').strip('/')
        
        sitemaps = discover_sitemaps(domain_clean)
        
        if not sitemaps:
            raise Exception("Không tìm thấy sitemap")

        for sitemap_url in sitemaps:
            sitemap_start = time.time()
            try:
                urls = parse_sitemap(sitemap_url)
                sitemap_duration = time.time() - sitemap_start
                
                unique_urls = list(set(urls))
                all_urls.update(unique_urls)
                
                sitemaps_data.append({
                    "sitemap": sitemap_url,
                    "count": len(unique_urls),
                    "duration": round(sitemap_duration, 2),
                    "urls": unique_urls
                })
                
            except Exception as e:
                sitemaps_data.append({
                    "sitemap": sitemap_url,
                    "count": 0,
                    "duration": round(time.time() - sitemap_start, 2),
                    "error": str(e)
                })

        total_duration = time.time() - start_time
        total_urls = len(all_urls)
        
        # Save enhanced history
        session_id = save_enhanced_history(
            domain=domain_clean,
            status="success",
            total_urls=total_urls,
            duration=total_duration,
            sitemaps_data=sitemaps_data,
            sample_urls=list(all_urls)[:100]  # Save sample of URLs
        )
        
        return {
            "domain": domain_clean,
            "status": "success",
            "total_urls": total_urls,
            "duration": total_duration,
            "sitemaps": sitemaps_data,
            "session_id": session_id
        }
        
    except Exception as e:
        total_duration = time.time() - start_time
        
        # Save failed session
        save_enhanced_history(
            domain=domain,
            status="failed",
            duration=total_duration,
            error_message=str(e),
            sitemaps_data=sitemaps_data
        )
        
        return {
            "domain": domain,
            "status": "failed",
            "error": str(e),
            "duration": total_duration
        }

# Routes
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

# Enhanced History API endpoints
@app.route('/api/history')
def get_enhanced_history():
    """Get history with advanced filtering"""
    
    limit = min(int(request.args.get("limit", 20)), 100)
    offset = max(int(request.args.get("offset", 0)), 0)
    domain_filter = request.args.get("domain")
    status_filter = request.args.get("status")
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    
    try:
        result = history_manager.get_history(
            limit=limit,
            offset=offset,
            domain_filter=domain_filter,
            status_filter=status_filter,
            date_from=date_from,
            date_to=date_to
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"Lỗi đọc lịch sử: {str(e)}"}), 500

@app.route('/api/history/statistics')
def get_crawl_statistics():
    """Get comprehensive crawl statistics"""
    
    days = min(int(request.args.get("days", 30)), 365)
    
    try:
        stats = history_manager.get_statistics(days=days)
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": f"Lỗi tạo thống kê: {str(e)}"}), 500

@app.route('/api/history/compare/<domain>')
def compare_domain_crawls(domain):
    """Compare recent crawls for a domain"""
    
    limit = min(int(request.args.get("limit", 5)), 20)
    
    try:
        comparison = history_manager.compare_crawls(domain, limit)
        return jsonify(comparison)
    except Exception as e:
        return jsonify({"error": f"Lỗi so sánh: {str(e)}"}), 500

@app.route('/api/history/export')
def export_history():
    """Export crawl history"""
    
    export_format = request.args.get("format", "csv")
    days = min(int(request.args.get("days", 30)), 365)
    
    try:
        # Get recent history
        history_data = history_manager.get_history(limit=1000, offset=0)
        
        if export_format == "json":
            return jsonify(history_data)
        
        # CSV export
        output = StringIO()
        writer = csv.writer(output)
        
        # Headers
        writer.writerow([
            "Domain", "Timestamp", "Status", "Total URLs", 
            "Duration (s)", "Sitemaps Found", "Error Message"
        ])
        
        # Data
        for record in history_data["results"]:
            writer.writerow([
                record["domain"],
                record["timestamp"],
                record["status"],
                record["total_urls"],
                record["duration_sec"],
                record["sitemaps_found"],
                record["error_message"] or ""
            ])
        
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=crawl_history.csv"}
        )
        
    except Exception as e:
        return jsonify({"error": f"Lỗi export: {str(e)}"}), 500

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