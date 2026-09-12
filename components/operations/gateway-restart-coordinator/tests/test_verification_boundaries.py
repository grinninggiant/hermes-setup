import hashlib
import json
from pathlib import Path
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import unittest
from restart_coordinator import ProcessRuntime, _coordinate_valid

class VerificationBoundaryTests(unittest.TestCase):
    def test_health_redirect_is_not_followed(self):
        hits=[]
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                if self.path=='/health':
                    self.send_response(302)
                    self.send_header('Location','/other')
                    self.end_headers()
                else:
                    self.send_response(200);self.end_headers();self.wfile.write(b'{}')
            def log_message(self,format,*args):pass
        server=HTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with self.assertRaises(Exception):
                ProcessRuntime().health(f'http://127.0.0.1:{server.server_port}/health')
            self.assertEqual(hits,['/health'])
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)

    def test_manifest_referenced_file_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();target=root/'component.py';target.write_text('original')
            manifest=root/'coordinate.json'
            manifest.write_text(json.dumps({'artifact':str(target),'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'profile':'general','kind':'candidate'}))
            payload={'artifact_path':str(manifest),'artifact_sha256':hashlib.sha256(manifest.read_bytes()).hexdigest()}
            self.assertTrue(_coordinate_valid(payload,'artifact'))
            target.write_text('changed')
            self.assertFalse(_coordinate_valid(payload,'artifact'))

if __name__=='__main__':unittest.main()
