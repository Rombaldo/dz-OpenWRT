# Общие помощники для сетевых действий скриптов роутера (install.sh, remote.sh, push.sh). Подключается командой «. netlib.sh».
# Скрипт install.sh запускают прямо из интернета, подключить этот файл он не может, поэтому в нём лежит копия блока между
# метками «>>> netlib» и «<<< netlib»; тест следит, чтобы копия и этот файл не разошлись. Меняйте оба места вместе.
#
# Зачем: у некоторых провайдеров (чаще у мобильного интернета) закрыты GitHub и репозиторий пакетов: «Connection refused»,
# а страницы из разрешённого списка открываются. Если на роутере работает прокси (SSClash/Clash, порт 7890), загрузку можно
# провести через него. Настройки роутера при этом не меняются: прокси подставляется только в окружение самой команды установки.
# >>> netlib
PROXY_HP="${DNEVNIK_PROXY:-127.0.0.1:7890}"
NETLIB_SET_PROXY=0

proxy_listening() { netstat -ltn 2>/dev/null | grep -q ":${PROXY_HP##*:} "; }
proxy_in_env() { [ -n "${https_proxy}${HTTPS_PROXY}${http_proxy}${HTTP_PROXY}" ]; }
use_proxy() {
    https_proxy="http://$PROXY_HP"; http_proxy="http://$PROXY_HP"; HTTPS_PROXY="http://$PROXY_HP"; HTTP_PROXY="http://$PROXY_HP"
    export https_proxy http_proxy HTTPS_PROXY HTTP_PROXY
    NETLIB_SET_PROXY=1
}
# Убрать прокси, который подставили мы (чужой, заданный человеком, не трогаем): дальше скрипт ходит как обычно.
drop_proxy() {
    [ "$NETLIB_SET_PROXY" = "1" ] || return 0
    unset https_proxy http_proxy HTTPS_PROXY HTTP_PROXY
    NETLIB_SET_PROXY=0
}
# net_try КОМАНДА…: выполняет команду; если прямой доступ не вышел, а на роутере слушает прокси, повторяет через него.
net_try() {
    "$@" && return 0
    if proxy_in_env || ! proxy_listening; then return 1; fi
    echo "!!  Прямое подключение не получилось. На роутере работает прокси ($PROXY_HP): повторяю через него (только для этой установки, настройки роутера не меняются)."
    use_proxy
    "$@"
}
# Запрос к самому роутеру всегда напрямую: прокси из окружения для 127.0.0.1 не годится.
local_get() { ( unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY; wget -q -T 3 -O /dev/null "$1" 2>/dev/null ); }
# Адрес роутера в домашней сети. Сначала то, что роутер реально получил (работает и когда адрес выдан по DHCP), потом настройка.
lan_ip() {
    ip=$(ubus call network.interface.lan status 2>/dev/null | jsonfilter -e '@["ipv4-address"][0].address' 2>/dev/null) || ip=""
    [ -n "$ip" ] || ip=$(uci -q get network.lan.ipaddr 2>/dev/null | cut -d/ -f1) || ip=""
    echo "$ip"
}
# name_resolves ИМЯ АДРЕС: DNS роутера отдаёт для имени именно этот адрес. Без известного адреса проверить нечего: ответ «нет».
name_resolves() { [ -n "$2" ] && nslookup "$1" 127.0.0.1 2>/dev/null | grep -qwF "$2"; }
# <<< netlib
