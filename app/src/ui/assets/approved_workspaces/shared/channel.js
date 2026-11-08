'use strict';

function connectWorkspace(receive) {
  const waiting = new Map();
  let sequence = 0;
  return new Promise(resolve => {
    new QWebChannel(qt.webChannelTransport, channel => {
      const bridge = channel.objects.workspace;
      bridge.received.connect((event, payload) => receive(event, JSON.parse(payload)));
      bridge.replied.connect((token, payload) => {
        const callback = waiting.get(token);
        waiting.delete(token);
        if (callback) callback(JSON.parse(payload));
      });
      resolve({
        ready: () => bridge.ready(),
        call: (action, args = {}) => new Promise((resolveCall, rejectCall) => {
          const token = String(++sequence);
          waiting.set(token, reply => {
            if (reply.ok) resolveCall(reply.value);
            else rejectCall(new Error(reply.message));
          });
          bridge.request(token, action, JSON.stringify(args));
        }),
      });
    });
  });
}
