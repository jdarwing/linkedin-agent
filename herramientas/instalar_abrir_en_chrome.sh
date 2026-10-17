#!/bin/bash
# instalar_abrir_en_chrome.sh — Se corre UNA vez en la Mac.
#
# Crea la mini app ~/Applications/AbrirEnChrome.app y la registra para los enlaces
# googlechromes://, que son los del botón "💻 Mac" del correo del agente.
# Al tocar uno en Mail de Apple, el perfil se abre en Google Chrome aunque el navegador
# predeterminado sea otro. Por seguridad solo abre direcciones de www.linkedin.com.
# En el iPhone se usa el botón "📱 iPhone", que abre la app de LinkedIn: no hace falta instalar nada.
#
# Para desinstalar: borrar ~/Applications/AbrirEnChrome.app

set -euo pipefail
APP="$HOME/Applications/AbrirEnChrome.app"
TMP="$(mktemp -d)"
mkdir -p "$HOME/Applications"

if [ ! -d "/Applications/Google Chrome.app" ] && [ ! -d "$HOME/Applications/Google Chrome.app" ]; then
  echo "No encontré Google Chrome instalado. Instálalo y vuelve a correr este script."; exit 1
fi

cat > "$TMP/abrir.applescript" <<'APPLESCRIPT'
on open location theURL
	set prefijo to "googlechromes://www.linkedin.com/"
	if theURL starts with prefijo then
		set destino to "https://" & (text 17 thru -1 of theURL)
		do shell script "open -a 'Google Chrome' " & quoted form of destino
	end if
end open location

on run
	display dialog "AbrirEnChrome está instalada. Abre en Chrome los enlaces de perfiles del agente de LinkedIn." buttons {"OK"} default button "OK"
end run
APPLESCRIPT

rm -rf "$APP"
osacompile -o "$APP" "$TMP/abrir.applescript"
PL="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier pe.adr.abrirenchrome" "$PL" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string pe.adr.abrirenchrome" "$PL"
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$PL"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes array" "$PL"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0 dict" "$PL"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLName string Perfil de LinkedIn en Chrome" "$PL"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes array" "$PL"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes:0 string googlechromes" "$PL"
codesign --force --sign - "$APP" >/dev/null 2>&1 || true
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP"
rm -rf "$TMP"

echo "Listo. Prueba: se abrirá un perfil de LinkedIn en Chrome."
open "googlechromes://www.linkedin.com/feed/"
