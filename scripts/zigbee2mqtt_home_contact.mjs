// SNZB-04P: forward actual IAS notifications before state caching.
// Install as a Zigbee2MQTT external extension together with the Home adapter.
// HomeMQTT selects one configured friendly_name and verifies its discovered IEEE.
// Keep this extension portable; it never decides which room owns an event.

export default class SpicaHomeContact {
    constructor(zigbee, mqtt, state, publishEntityState, eventBus) {
        this.mqtt = mqtt;
        this.eventBus = eventBus;
        this.active = false;
    }

    start() {
        this.active = true;
        this.eventBus.onDeviceMessage(this, async ({device, cluster, type, data}) => {
            if (!this.active || !this.mqtt.isConnected()
                || !/^0x[0-9a-f]{16}$/.test(device.ieeeAddr)
                || device.definition?.model !== 'SNZB-04P' || !device.interviewed
                || cluster !== 'ssIasZone' || type !== 'commandStatusChangeNotification'
                || !Number.isInteger(data?.zonestatus) || data.zonestatus < 0 || data.zonestatus > 65535) {
                return;
            }
            await this.mqtt.publish(`${device.name}/home/contact`, JSON.stringify({
                ieee_address: device.ieeeAddr,
                contact: (data.zonestatus & 1) === 0,
                observed_at: Date.now() / 1000,
            }), {clientOptions: {qos: 0, retain: false}});
        });
    }

    stop() {
        this.active = false;
        this.eventBus.removeListeners(this);
    }
}
