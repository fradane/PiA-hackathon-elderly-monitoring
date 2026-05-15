from arduino.app_utils import App, Bridge
import time
import requests

def loop() -> None:
    #print("Hello")
    time.sleep(3)

def receive_event(sensor: str, value: bool):
    requests.post(
        "http://localhost:5001/event",
        json={
            "sensor": sensor,
            "value": value,
            "timestamp": time.time(),
        },
    )

if __name__ == "__main__":
    
    # BRIDGE setup
    Bridge.provide("receive_event", receive_event)
    # END setup 
    
    App.run(user_loop=loop)  # This will block until the app is stopped
