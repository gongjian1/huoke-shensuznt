#!/bin/bash
# 获客系统部署脚本（Ubuntu / Debian，需要 root）
# 用法：sudo bash deploy.sh 你的域名.com
# 会做：装依赖 → 复制代码到 /opt/huoke → 建虚拟环境 → 生成主密钥 → 注册 systemd 服务 → 配 nginx。
# HTTPS 证书用 certbot 申请，域名需要先解析到本机。
# 如果你用的是 1Panel / 宝塔，请跳过 nginx 那一步，自己在面板里把域名反代到 127.0.0.1:5001。
set -e

DOMAIN="${1:?用法: sudo bash deploy.sh 你的域名.com}"
APP_DIR=/opt/huoke
PORT=5001
SRC="$(cd "$(dirname "$0")" && pwd)"

echo "[1/6] 安装系统依赖"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip nginx certbot python3-certbot-nginx

echo "[2/6] 复制代码到 $APP_DIR（不会复制任何 .db 数据库）"
mkdir -p "$APP_DIR"
cp "$SRC"/*.py "$SRC/requirements.txt" "$APP_DIR/"
rm -rf "$APP_DIR/templates" "$APP_DIR/static"
cp -r "$SRC/templates" "$SRC/static" "$APP_DIR/"

echo "[3/6] 创建 Python 虚拟环境"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "[4/6] 生成主密钥 /etc/huoke.env（已存在则不动）"
if [ ! -f /etc/huoke.env ]; then
    SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
    cat > /etc/huoke.env <<EOF
HUOKE_SECRET=$SECRET
DATA_API_BASE=
DATA_API_KEY=
HUOKE_SCHEDULER=1
HUOKE_BEHIND_PROXY=1
HUOKE_HTTPS=1
EOF
    chmod 600 /etc/huoke.env
    echo "  已生成。请备份这个文件：丢了它，数据库里客户保存的阿里云 Key 就解不开了。"
fi

echo "[5/6] 注册 systemd 服务"
id -u huoke >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin huoke
chown -R huoke:huoke "$APP_DIR"
cat > /etc/systemd/system/huoke.service <<UNIT
[Unit]
Description=Huoke lead system
After=network.target

[Service]
User=huoke
WorkingDirectory=$APP_DIR
EnvironmentFile=/etc/huoke.env
ExecStart=$APP_DIR/venv/bin/gunicorn -w 1 --threads 8 -b 127.0.0.1:$PORT app:app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now huoke
sleep 2
systemctl is-active --quiet huoke && echo "  服务已启动" || { echo "  启动失败：journalctl -u huoke -n 50"; exit 1; }

echo "[6/6] 配置 nginx"
cat > "/etc/nginx/sites-available/$DOMAIN" <<NGINX
server {
    listen 80;
    server_name $DOMAIN;
    client_max_body_size 8m;
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300;
    }
}
NGINX
ln -sf "/etc/nginx/sites-available/$DOMAIN" "/etc/nginx/sites-enabled/$DOMAIN"
nginx -t && nginx -s reload
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email --redirect \
    || echo "  证书申请失败（多半是域名还没解析到本机）。解析好后手动运行：certbot --nginx -d $DOMAIN"

cat <<MSG

部署完成。接下来还差两步：
  1. 设置管理员密码（登录名 admin）：
       cd $APP_DIR && sudo -u huoke $APP_DIR/venv/bin/python manage.py set-admin-password
  2. 填数据接口地址和 Key（向作者获取）：登录后「管理后台 → 系统设置」，或写进 /etc/huoke.env 的 DATA_API_BASE / DATA_API_KEY 后 systemctl restart huoke
常用：systemctl status huoke / journalctl -u huoke -f
MSG
