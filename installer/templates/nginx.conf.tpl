# Domarc SMTP Relay — nginx reverse proxy
# Generato dall'installer; per modifiche permanenti edita
# /opt/domarc-smtp-relay-admin/installer/templates/nginx.conf.tpl
# e rilancia 03-nginx-https.sh.

server {
    listen 80;
    server_name __SERVER_NAME__;

    # ACME challenge per Let's Encrypt
    location /.well-known/acme-challenge/ {
        root /var/www/letsencrypt;
    }

    # Redirect tutto il resto in HTTPS
    location / {
        return 301 https://$host$request_uri;
    }
}

server {
    listen __ADMIN_PORT__ ssl http2;
    server_name __SERVER_NAME__;

    # Cert paths sostituiti dall'installer in base alla modalità
    ssl_certificate     __SSL_CERT__;
    ssl_certificate_key __SSL_KEY__;

    # TLS hardening
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305;
    ssl_prefer_server_ciphers on;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 1d;
    ssl_session_tickets off;

    # HSTS (1 anno; comment out se ancora in cert testing)
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "no-referrer-when-downgrade" always;

    # Body size: upload allegati template fino a 10 MB
    client_max_body_size 12M;

    # Reverse proxy verso Flask gunicorn locale
    location / {
        proxy_pass http://127.0.0.1:5443;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Host $host;
        proxy_redirect off;

        # Buffering off per streaming (Activity Live polling)
        proxy_buffering off;
        proxy_http_version 1.1;
        proxy_read_timeout 60s;
        proxy_connect_timeout 10s;
    }

    # Logging
    access_log /var/log/nginx/domarc-relay.access.log;
    error_log  /var/log/nginx/domarc-relay.error.log warn;
}
