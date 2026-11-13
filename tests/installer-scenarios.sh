#!/bin/sh
set -eu
mode=$1
mkdir -p /testbin /etc/nginx /etc/letsencrypt/live/test /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/nginx/site.conf <<'EOF'
server {
    listen 80;
    server_name cloud.example.com;
    return 301 https://$host$request_uri;
}
server {
    listen 443 ssl http2;
    server_name cloud.example.com;
    location / { proxy_pass http://127.0.0.1:9200; }
}
server {
    listen 443 quic reuseport;
    listen [::]:443 quic reuseport;
    server_name other.example.com;
    return 200 ok;
}
EOF
if [ "$mode" = free ]; then
  sed -i '/listen 443 quic/d; /listen \[::\]:443 quic/d' /etc/nginx/site.conf
fi
cp /etc/nginx/site.conf /tmp/original.conf
printf '%s\n' dummy >/etc/letsencrypt/live/test/fullchain.pem
printf '%s\n' dummy >/etc/letsencrypt/live/test/privkey.pem
printf '%s\n' 'ssl_session_cache shared:le_nginx_SSL:10m;' >/etc/letsencrypt/options-ssl-nginx.conf
printf '#!/bin/sh\nexit 0\n' >/testbin/rpxy
chmod +x /testbin/rpxy
mkdir -p /root/opencloud/rpxy-h3
cp /testbin/rpxy /root/opencloud/rpxy-h3/rpxy-bin
cat > /testbin/mock <<'EOF'
#!/bin/sh
case "$(basename "$0")" in
  nginx)
    case "${1:-}" in
      -T) printf '# configuration file /etc/nginx/site.conf:\n'; cat /etc/nginx/site.conf; printf '# configuration file /etc/letsencrypt/options-ssl-nginx.conf:\n'; cat /etc/letsencrypt/options-ssl-nginx.conf;;
      -t) exit 0;;
      -s)
        if grep -q '127.0.0.1:18443' /etc/nginx/site.conf; then touch /tmp/nginx-moved; else rm -f /tmp/nginx-moved; fi
        exit 0;;
    esac;;
  ss)
    if [ -f /tmp/nginx-moved ]; then
      case "${LAB_MODE:-}" in
        linger-decline|linger-restart) [ -f /tmp/nginx-restarted ] && exit 0;;
        *) exit 0;;
      esac
    fi
    if [ "${LAB_MODE:-}" = free ]; then
      exit 0
    elif [ "${LAB_MODE:-}" = unsupported ]; then
      printf 'UNCONN 0 0 0.0.0.0:443 0.0.0.0:* users:(("otherd",pid=1,fd=3))\n'
    else
      printf 'UNCONN 0 0 0.0.0.0:443 0.0.0.0:* users:(("nginx",pid=1,fd=3))\n'
    fi;;
  openssl)
    case " $* " in
      *' -pubkey '*) printf 'DUMMY_PUBLIC_KEY';;
      *' -pubin '*) cat;;
      *' -pubout '*) printf 'DUMMY_PUBLIC_KEY';;
      *) exit 0;;
    esac;;
  docker)
    case "${1:-}" in
      inspect)
        for last do :; done
        [ -f "/tmp/mock-container-$last" ] || exit 1
        cat "/tmp/mock-container-$last";;
      run)
        case " $* " in
          *' --entrypoint curl '*)
            case " $* " in
              *' cloud.example.com:443:127.0.0.1 '*) [ "${LAB_MODE:-}" != failure ] || exit 1;;
            esac
            printf '200';;
          *)
            prev=''
            for arg do
              if [ "$prev" = '--name' ]; then printf 'true\n' >"/tmp/mock-container-$arg"; fi
              prev=$arg
            done
            printf 'dummy-container-id\n';;
        esac;;
      rm)
        for last do :; done
        rm -f "/tmp/mock-container-$last";;
      *) exit 0;;
    esac;;
  curl) printf 'HTTP/2 200\nAlt-Svc: h3=":443"; ma=60\n\n';;
  systemctl)
    if [ "${1:-}" = restart ]; then touch /tmp/nginx-restarted; fi
    exit 0;;
esac
EOF
chmod +x /testbin/mock
for x in nginx ss openssl docker curl systemctl; do ln -s /testbin/mock "/testbin/$x"; done
export PATH="/testbin:$PATH" LAB_MODE="$mode"
set +e
if [ "$mode" = linger-decline ]; then
  printf '\n\n\nAPPLY\n\n' | python3 /fixtures/setup.py
elif [ "$mode" = linger-restart ]; then
  printf '\n\n\nAPPLY\nRESTART\n' | python3 /fixtures/setup.py
elif [ "$mode" = free ]; then
  printf '\n\nAPPLY\n' | python3 /fixtures/setup.py
else
  printf '\n\n\nAPPLY\n' | python3 /fixtures/setup.py
fi
status=$?
set -e
case "$mode" in
 success|capture|linger-restart|retry)
   [ "$status" -eq 0 ]
   grep -q 'listen 127.0.0.1:18443 quic' /etc/nginx/site.conf
   grep -q 'listen \[::1\]:18443 quic' /etc/nginx/site.conf
   grep -q 'add_header Alt-Svc' /etc/nginx/site.conf
   test -x /etc/letsencrypt/renewal-hooks/deploy/opencloud-h3-experimental
   if [ "$mode" = capture ]; then
     mkdir -p /lab/generated
     cp /var/lib/opencloud-h3-experimental/stage.toml /var/lib/opencloud-h3-experimental/router.toml /var/lib/opencloud-h3-experimental/rpxy.toml /lab/generated/
   fi
   printf '%s\n' renewed >/etc/letsencrypt/live/test/fullchain.pem
   RENEWED_LINEAGE=/etc/letsencrypt/live/test /etc/letsencrypt/renewal-hooks/deploy/opencloud-h3-experimental
   grep -q renewed /var/lib/opencloud-h3-experimental/certs/fullchain.pem
   python3 /fixtures/setup.py --rollback
   cmp /tmp/original.conf /etc/nginx/site.conf
   test ! -e /etc/letsencrypt/renewal-hooks/deploy/opencloud-h3-experimental
   test ! -e /var/lib/opencloud-h3-experimental/certs/privkey.pem
   test ! -e /tmp/mock-container-opencloud-h3-experimental-router
   test ! -e /tmp/mock-container-opencloud-h3-experimental-rpxy
   if [ "$mode" = retry ]; then
     printf '\n\n\nAPPLY\n' | python3 /fixtures/setup.py
     test "$(find /var/lib -maxdepth 1 -name 'opencloud-h3-experimental.rolled-back-*' | wc -l)" -eq 1
     python3 /fixtures/setup.py --rollback
     cmp /tmp/original.conf /etc/nginx/site.conf
     printf 'PASS: retry archived the verified previous rollback\n'
   fi
   printf 'PASS: interactive install and manual rollback restored Nginx\n';;
 failure|linger-decline)
   [ "$status" -ne 0 ]
   cmp /tmp/original.conf /etc/nginx/site.conf
   test -f /var/lib/opencloud-h3-experimental/ROLLED_BACK
   test ! -e /var/lib/opencloud-h3-experimental/certs/privkey.pem
   test ! -e /tmp/mock-container-opencloud-h3-experimental-router
   test ! -e /tmp/mock-container-opencloud-h3-experimental-rpxy
   test -s /var/lib/opencloud-h3-experimental/diagnostics.json
   printf 'PASS: injected post-switch failure triggered automatic rollback\n';;
 unsupported)
   [ "$status" -ne 0 ]
   cmp /tmp/original.conf /etc/nginx/site.conf
   test ! -e /var/lib/opencloud-h3-experimental
   printf 'PASS: non-Nginx UDP owner rejected without changes\n';;
 free)
   [ "$status" -eq 0 ]
   grep -q 'add_header Alt-Svc' /etc/nginx/site.conf
   python3 /fixtures/setup.py --rollback
   cmp /tmp/original.conf /etc/nginx/site.conf
   printf 'PASS: free UDP port path installed and rolled back\n';;
 modified)
   [ "$status" -eq 0 ]
   printf '# external edit\n' >>/etc/nginx/site.conf
   if python3 /fixtures/setup.py --rollback; then exit 1; fi
   grep -q '# external edit' /etc/nginx/site.conf
   test ! -e /var/lib/opencloud-h3-experimental/ROLLED_BACK
   printf 'PASS: rollback refused to overwrite an externally modified config\n';;
 foreign)
   [ "$status" -eq 0 ]
   printf 'false\n' >/tmp/mock-container-opencloud-h3-experimental-router
   if python3 /fixtures/setup.py --rollback; then exit 1; fi
   test -e /tmp/mock-container-opencloud-h3-experimental-router
   test ! -e /var/lib/opencloud-h3-experimental/ROLLED_BACK
   printf 'PASS: rollback refused to remove a container with a foreign label\n';;
esac
