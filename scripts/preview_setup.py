"""Loopback-only UI fixture. Does not configure hardware or contact OpenAI."""
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import json, time
PAGE = Path(__file__).resolve().parents[1] / 'main/setup.html'
state = dict(connected=False, joining=False, key_saved=False, settings_saved=False, firmware_version='preview-only', message='Preview: choose your Wi-Fi.', token='preview-only')
due = 0
outcome = False
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def reply(self,body,status=200,kind='application/json'):
        data=body.encode() if isinstance(body,str) else json.dumps(body).encode()
        self.send_response(status); self.send_header('Content-Type',kind); self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(data)
    def do_GET(self):
        global due
        if self.path=='/status':
            if due and time.monotonic()>=due:
                state.update(joining=False,connected=outcome,settings_saved=outcome,message='Preview: connected! Your settings are saved.' if outcome else 'Preview: could not connect. Check password or use a 2.4 GHz network.');due=0
            self.reply(state)
        elif self.path=='/networks': self.reply([dict(ssid='Home Wi-Fi',rssi=-43,open=False,enterprise=False),dict(ssid='Guest <&> network',rssi=-65,open=True,enterprise=False),dict(ssid='Office enterprise',rssi=-70,open=False,enterprise=True)])
        else:self.reply(PAGE.read_text().replace('__FIRMWARE_VERSION__','preview-only'),kind='text/html; charset=utf-8')
    def do_POST(self):
        global due,outcome
        if self.headers.get('X-Setup-Token')!='preview-only':return self.reply('Forbidden',403,'text/plain')
        if self.path=='/configure':
            body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))))
            outcome=body.get('password')!='wrongpass';due=time.monotonic()+3
            state.update(joining=True,connected=False,message='Preview: connecting…');self.reply({})
        elif self.path=='/finish':self.reply({})
        else:self.reply({},404)
if __name__=='__main__':
    print('UI FIXTURE ONLY: http://127.0.0.1:8766 — no real Wi-Fi or API calls',flush=True)
    HTTPServer(('127.0.0.1',8766),Handler).serve_forever()
