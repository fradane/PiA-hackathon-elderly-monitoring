#include "Arduino_RouterBridge.h"

void setup() {

    // BRIDGE setup
    Bridge.begin();
    // END setup
    
}

void loop() {

  Bridge.notify("receive_event", stringa, bool);
  
  delay(1000);
}
