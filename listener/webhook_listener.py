from http.server import HTTPServer, BaseHTTPRequestHandler
import json

class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        print("\n=== WEBHOOK RECIBIDO ===")
        print(json.dumps(json.loads(body), indent=2))
        self.send_response(200)
        self.end_headers()

server = HTTPServer(('localhost', 3000), WebhookHandler)
print("Webhook escuchando en http://localhost:3000")
server.serve_forever()