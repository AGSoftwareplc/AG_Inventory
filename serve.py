"""Start AG Inventory for daily use in the shop (Waitress server).

Other computers, tablets and phones on the same Wi-Fi open http://<this-PC-IP>:5000
"""
import os
import socket
import threading
import webbrowser

from waitress import serve

from app import create_app

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
        s.close()
    except OSError:
        ip = '127.0.0.1'
    print('AG Inventory is running.')
    print(f'  On this computer:      http://localhost:{port}')
    print(f'  Other devices (Wi-Fi): http://{ip}:{port}')
    print('Keep this window open. Press Ctrl+C to stop.')
    app = create_app()
    if os.environ.get('AGINV_NO_BROWSER') != '1':
        # open the app in the browser once the server has had a moment to start
        threading.Timer(1.5, lambda: webbrowser.open(f'http://localhost:{port}')).start()
    serve(app, host='0.0.0.0', port=port, threads=8)
