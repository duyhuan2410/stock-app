# File: app.py
from flask import Flask, jsonify, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_bcrypt import Bcrypt
from datetime import datetime, timedelta
import psycopg2
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = '93d2f15a069cfb9bb57ad35ca3f8fbe9'

# Cấu hình PostgreSQL từ biến môi trường (Render sẽ cung cấp DATABASE_URL)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'postgresql://airflow:airflow@localhost:5432/stock_data')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Khởi tạo các extension
db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'index'

# Cấu hình kết nối database PostgreSQL cho dữ liệu chứng khoán
# Khi triển khai trên Render, sử dụng DATABASE_URL để kết nối
def connect_db():
    try:
        database_url = os.getenv('DATABASE_URL')
        if not database_url:
            raise Exception("DATABASE_URL not set")
        # DATABASE_URL của Render có dạng postgresql://user:password@host:port/dbname
        # psycopg2 cần được cấu hình lại từ URL này
        return psycopg2.connect(database_url, sslmode='require')
    except Exception as e:
        print(f"Error connecting to database: {e}")
        raise

# Model người dùng
class User(UserMixin, db.Model):
    __tablename__ = 'users'  # Đặt tên bảng trong PostgreSQL
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(120), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password = bcrypt.generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        return bcrypt.check_password_hash(self.password, password)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Tạo bảng trong cơ sở dữ liệu
with app.app_context():
    try:
        db.create_all()
        print("User database tables created successfully.")
    except Exception as e:
        print(f"Error creating user database tables: {str(e)}")

# Route chính (trang index, xử lý đăng nhập và đăng ký)
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'register':
            username = request.form.get('username')
            password = request.form.get('password')

            if User.query.filter_by(username=username).first():
                flash('Tên người dùng đã tồn tại. Vui lòng chọn tên khác.', 'error')
                return redirect(url_for('index'))

            new_user = User(username=username)
            new_user.set_password(password)
            db.session.add(new_user)
            db.session.commit()

            flash('Đăng ký thành công! Vui lòng đăng nhập.', 'success')
            return redirect(url_for('index'))

        elif action == 'login':
            username = request.form.get('username')
            password = request.form.get('password')

            user = User.query.filter_by(username=username).first()
            if user and user.check_password(password):
                login_user(user)
                return redirect(url_for('index'))
            else:
                flash('Tên người dùng hoặc mật khẩu không đúng.', 'error')
                return redirect(url_for('index'))

    return render_template('index.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

# API để lấy dữ liệu cổ phiếu
@app.route('/api/stock/<symbol>/<interval>/<period>', methods=['GET'])
@login_required
def get_stock_data(symbol, interval, period):
    conn = connect_db()
    cursor = conn.cursor()
    
    try:
        cursor.execute("SELECT MAX(timestamp) FROM CombinedStockPrices WHERE stock_symbol = %s", (symbol,))
        latest_timestamp = cursor.fetchone()[0]

        if latest_timestamp is None:
            return jsonify({"error": f"No data available for {symbol} in CombinedStockPrices"}), 404

        if not isinstance(latest_timestamp, datetime):
            latest_timestamp = datetime.now()

        earliest_timestamp = datetime.strptime('2010-01-01 00:00:00', '%Y-%m-%d %H:%M:%S')

        if period == "3mo":
            start_date = latest_timestamp - timedelta(days=90)
        elif period == "1y":
            start_date = latest_timestamp - timedelta(days=365)
        elif period == "3y":
            start_date = latest_timestamp - timedelta(days=365 * 3)
        elif period == "5y":
            start_date = latest_timestamp - timedelta(days=365 * 5)
        else:
            start_date = earliest_timestamp

        if interval == "1m":
            query = """
                SELECT timestamp, open_price, high_price, low_price, close_price, volume,
                       sma10, sma20, rsi, bb_upper, bb_middle, bb_lower, macd
                FROM CombinedStockPrices
                WHERE stock_symbol = %s AND timestamp >= %s AND timestamp <= %s
                ORDER BY timestamp ASC
            """
            cursor.execute(query, (symbol, start_date, latest_timestamp))
        elif interval == "1d":
            query = """
                WITH DailyData AS (
                    SELECT 
                        date_trunc('day', timestamp) AS time_bucket,
                        FIRST_VALUE(open_price) OVER w AS open_price,
                        MAX(high_price) OVER w AS high_price,
                        MIN(low_price) OVER w AS low_price,
                        LAST_VALUE(close_price) OVER w AS close_price,
                        SUM(volume) OVER w AS volume,
                        LAST_VALUE(sma10) OVER w AS sma10,
                        LAST_VALUE(sma20) OVER w AS sma20,
                        LAST_VALUE(rsi) OVER w AS rsi,
                        LAST_VALUE(bb_upper) OVER w AS bb_upper,
                        LAST_VALUE(bb_middle) OVER w AS bb_middle,
                        LAST_VALUE(bb_lower) OVER w AS bb_lower,
                        LAST_VALUE(macd) OVER w AS macd,
                        ROW_NUMBER() OVER w AS rn
                    FROM CombinedStockPrices
                    WHERE stock_symbol = %s AND timestamp >= %s AND timestamp <= %s
                    WINDOW w AS (
                        PARTITION BY date_trunc('day', timestamp)
                        ORDER BY timestamp
                        ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                    )
                )
                SELECT time_bucket, open_price, high_price, low_price, close_price, volume,
                       sma10, sma20, rsi, bb_upper, bb_middle, bb_lower, macd
                FROM DailyData
                WHERE rn = 1
                ORDER BY time_bucket ASC
            """
            cursor.execute(query, (symbol, start_date, latest_timestamp))
        elif interval == "4h":
            query = """
                WITH FourHourData AS (
                    SELECT 
                        date_trunc('hour', timestamp) - 
                        (extract(hour from timestamp)::integer % 4) * interval '1 hour' AS time_bucket,
                        FIRST_VALUE(open_price) OVER w AS open_price,
                        MAX(high_price) OVER w AS high_price,
                        MIN(low_price) OVER w AS low_price,
                        LAST_VALUE(close_price) OVER w AS close_price,
                        SUM(volume) OVER w AS volume,
                        LAST_VALUE(sma10) OVER w AS sma10,
                        LAST_VALUE(sma20) OVER w AS sma20,
                        LAST_VALUE(rsi) OVER w AS rsi,
                        LAST_VALUE(bb_upper) OVER w AS bb_upper,
                        LAST_VALUE(bb_middle) OVER w AS bb_middle,
                        LAST_VALUE(bb_lower) OVER w AS bb_lower,
                        LAST_VALUE(macd) OVER w AS macd,
                        ROW_NUMBER() OVER w AS rn
                    FROM CombinedStockPrices
                    WHERE stock_symbol = %s AND timestamp >= %s AND timestamp <= %s
                    WINDOW w AS (
                        PARTITION BY date_trunc('hour', timestamp) - 
                        (extract(hour from timestamp)::integer % 4) * interval '1 hour'
                        ORDER BY timestamp
                        ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                    )
                )
                SELECT time_bucket, open_price, high_price, low_price, close_price, volume,
                       sma10, sma20, rsi, bb_upper, bb_middle, bb_lower, macd
                FROM FourHourData
                WHERE rn = 1
                ORDER BY time_bucket ASC
            """
            cursor.execute(query, (symbol, start_date, latest_timestamp))
        elif interval == "1w":
            query = """
                WITH WeeklyData AS (
                    SELECT 
                        date_trunc('week', timestamp) AS time_bucket,
                        FIRST_VALUE(open_price) OVER w AS open_price,
                        MAX(high_price) OVER w AS high_price,
                        MIN(low_price) OVER w AS low_price,
                        LAST_VALUE(close_price) OVER w AS close_price,
                        SUM(volume) OVER w AS volume,
                        LAST_VALUE(sma10) OVER w AS sma10,
                        LAST_VALUE(sma20) OVER w AS sma20,
                        LAST_VALUE(rsi) OVER w AS rsi,
                        LAST_VALUE(bb_upper) OVER w AS bb_upper,
                        LAST_VALUE(bb_middle) OVER w AS bb_middle,
                        LAST_VALUE(bb_lower) OVER w AS bb_lower,
                        LAST_VALUE(macd) OVER w AS macd,
                        ROW_NUMBER() OVER w AS rn
                    FROM CombinedStockPrices
                    WHERE stock_symbol = %s AND timestamp >= %s AND timestamp <= %s
                    WINDOW w AS (
                        PARTITION BY date_trunc('week', timestamp)
                        ORDER BY timestamp
                        ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                    )
                )
                SELECT time_bucket, open_price, high_price, low_price, close_price, volume,
                       sma10, sma20, rsi, bb_upper, bb_middle, bb_lower, macd
                FROM WeeklyData
                WHERE rn = 1
                ORDER BY time_bucket ASC
            """
            cursor.execute(query, (symbol, start_date, latest_timestamp))
        elif interval == "1mo":
            query = """
                WITH MonthlyData AS (
                    SELECT 
                        date_trunc('month', timestamp) AS time_bucket,
                        FIRST_VALUE(open_price) OVER w AS open_price,
                        MAX(high_price) OVER w AS high_price,
                        MIN(low_price) OVER w AS low_price,
                        LAST_VALUE(close_price) OVER w AS close_price,
                        SUM(volume) OVER w AS volume,
                        LAST_VALUE(sma10) OVER w AS sma10,
                        LAST_VALUE(sma20) OVER w AS sma20,
                        LAST_VALUE(rsi) OVER w AS rsi,
                        LAST_VALUE(bb_upper) OVER w AS bb_upper,
                        LAST_VALUE(bb_middle) OVER w AS bb_middle,
                        LAST_VALUE(bb_lower) OVER w AS bb_lower,
                        LAST_VALUE(macd) OVER w AS macd,
                        ROW_NUMBER() OVER w AS rn
                    FROM CombinedStockPrices
                    WHERE stock_symbol = %s AND timestamp >= %s AND timestamp <= %s
                    WINDOW w AS (
                        PARTITION BY date_trunc('month', timestamp)
                        ORDER BY timestamp
                        ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                    )
                )
                SELECT time_bucket, open_price, high_price, low_price, close_price, volume,
                       sma10, sma20, rsi, bb_upper, bb_middle, bb_lower, macd
                FROM MonthlyData
                WHERE rn = 1
                ORDER BY time_bucket ASC
            """
            cursor.execute(query, (symbol, start_date, latest_timestamp))
        else:
            return jsonify({"error": f"Unsupported interval: {interval}"}), 400

        rows = cursor.fetchall()
        if not rows:
            return jsonify({"error": f"No data found for {symbol} with interval {interval} and period {period}"}), 404
        
        data = []
        for row in rows:
            data.append({
                "timestamp": row[0].strftime('%Y-%m-%d %H:%M:%S'),
                "open": float(row[1]) if row[1] is not None else None,
                "high": float(row[2]) if row[2] is not None else None,
                "low": float(row[3]) if row[3] is not None else None,
                "close": float(row[4]) if row[4] is not None else None,
                "volume": float(row[5]) if row[5] is not None else None,
                "sma10": float(row[6]) if row[6] is not None else None,
                "sma20": float(row[7]) if row[7] is not None else None,
                "rsi": float(row[8]) if row[8] is not None else None,
                "bb_upper": float(row[9]) if row[9] is not None else None,
                "bb_middle": float(row[10]) if row[10] is not None else None,
                "bb_lower": float(row[11]) if row[11] is not None else None,
                "macd": float(row[12]) if row[12] is not None else None
            })

        return jsonify(data)
    except Exception as e:
        return jsonify({"error": f"Error querying stock data: {str(e)}"}), 500
    finally:
        cursor.close()
        conn.close()

# API để lấy dữ liệu chỉ số
@app.route('/api/index/<symbol>', methods=['GET'])
def get_index_data(symbol):
    conn = connect_db()
    cursor = conn.cursor()
    
    try:
        query_latest = """
            SELECT timestamp, close_price, high_price, low_price
            FROM IndexPrices
            WHERE index_symbol = %s
            ORDER BY timestamp DESC
            LIMIT 2
        """
        cursor.execute(query_latest, (symbol,))
        latest_rows = cursor.fetchall()
        
        if not latest_rows or len(latest_rows) < 2:
            return jsonify({"error": "Not enough data for this index"}), 404
        
        latest = latest_rows[0]
        previous = latest_rows[1]
        
        query_history = """
            SELECT timestamp, close_price
            FROM IndexPrices
            WHERE index_symbol = %s
            AND timestamp >= %s
            ORDER BY timestamp ASC
        """
        start_date = latest[0] - timedelta(days=30)
        cursor.execute(query_history, (symbol, start_date))
        history_rows = cursor.fetchall()

        history_data = [{"timestamp": row[0].strftime('%Y-%m-%d'), "close": float(row[1]) if row[1] is not None else None} for row in history_rows]

        data = {
            "timestamp": latest[0].strftime('%Y-%m-%d %H:%M:%S'),
            "close": float(latest[1]) if latest[1] is not None else None,
            "high": float(latest[2]) if latest[2] is not None else None,
            "low": float(latest[3]) if latest[3] is not None else None,
            "change": float(latest[1] - previous[1]) if latest[1] is not None and previous[1] is not None else None,
            "percent_change": float(((latest[1] - previous[1]) / previous[1]) * 100) if latest[1] is not None and previous[1] is not None and previous[1] != 0 else None,
            "history": history_data
        }
        
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": f"Error querying index data: {str(e)}"}), 500
    finally:
        cursor.close()
        conn.close()

# API để lấy thông tin công ty
@app.route('/api/company/<symbol>', methods=['GET'])
@login_required
def get_company_info(symbol):
    conn = connect_db()
    cursor = conn.cursor()
    
    try:
        query = """
            SELECT company_name, sector, industry, trailing_eps, forward_eps, trailing_pe,
                   forward_pe, roe, roa, market_cap, business_summary, logo_url
            FROM CompanyInfo
            WHERE stock_symbol = %s
        """
        cursor.execute(query, (symbol,))
        row = cursor.fetchone()
        
        if not row:
            return jsonify({"error": "No company info found"}), 404
        
        data = {
            "company_name": row[0],
            "sector": row[1],
            "industry": row[2],
            "trailing_eps": float(row[3]) if row[3] is not None else None,
            "forward_eps": float(row[4]) if row[4] is not None else None,
            "trailing_pe": float(row[5]) if row[5] is not None else None,
            "forward_pe": float(row[6]) if row[6] is not None else None,
            "roe": float(row[7]) if row[7] is not None else None,
            "roa": float(row[8]) if row[8] is not None else None,
            "market_cap": float(row[9]) if row[9] is not None else None,
            "business_summary": row[10],
            "logo_url": row[11] if row[11] else "https://via.placeholder.com/100?text=Logo"
        }
        
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": f"Error querying company info: {str(e)}"}), 500
    finally:
        cursor.close()
        conn.close()

# API để lấy dữ liệu tài chính
@app.route('/api/financials/<symbol>', methods=['GET'])
@login_required
def get_financial_data(symbol):
    conn = connect_db()
    cursor = conn.cursor()
    
    try:
        query = """
            SELECT year, net_income, total_revenue, gross_profit, operating_income, long_term_debt, free_cash_flow
            FROM CompanyFinancials
            WHERE stock_symbol = %s
            ORDER BY year DESC
        """
        cursor.execute(query, (symbol,))
        rows = cursor.fetchall()
        
        if not rows:
            return jsonify({"error": "No financial data found"}), 404
        
        data = [{
            "year": row[0],
            "net_income": float(row[1]) if row[1] is not None else None,
            "total_revenue": float(row[2]) if row[2] is not None else None,
            "gross_profit": float(row[3]) if row[3] is not None else None,
            "operating_income": float(row[4]) if row[4] is not None else None,
            "long_term_debt": float(row[5]) if row[5] is not None else None,
            "free_cash_flow": float(row[6]) if row[6] is not None else None
        } for row in rows]
        
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": f"Error querying financial data: {str(e)}"}), 500
    finally:
        cursor.close()
        conn.close()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)