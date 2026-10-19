import CoreBluetooth
import ExpoModulesCore

// Advertises one 128-bit service UUID that carries the trip's rotating Bluetooth ID.
// iOS only allows service UUIDs and a local name in advertisements (no service data), which is
// why the ID is encoded into the UUID itself. Advertising works while the app is in the foreground;
// in the background iOS moves service UUIDs to an overflow area that most scanners can't read.
public class TrustlaneBleModule: Module {
  private var manager: CBPeripheralManager?
  private var peripheralDelegate: PeripheralDelegate?
  private var pendingUuid: String?

  public func definition() -> ModuleDefinition {
    Name("TrustlaneBle")

    Events("onAdvertisingState")

    Function("isSupported") { () -> Bool in
      return true
    }

    Function("startAdvertising") { (serviceUuid: String) in
      self.pendingUuid = serviceUuid
      if self.manager == nil {
        let delegate = PeripheralDelegate(module: self)
        self.peripheralDelegate = delegate
        // Advertising starts once Bluetooth reports poweredOn (see the delegate).
        self.manager = CBPeripheralManager(delegate: delegate, queue: nil)
      } else {
        self.advertise()
      }
    }

    Function("stopAdvertising") {
      self.pendingUuid = nil
      self.manager?.stopAdvertising()
    }

    OnDestroy {
      self.manager?.stopAdvertising()
    }
  }

  func advertise() {
    guard let manager = manager, manager.state == .poweredOn, let uuid = pendingUuid else {
      return
    }
    manager.stopAdvertising()
    manager.startAdvertising([CBAdvertisementDataServiceUUIDsKey: [CBUUID(string: uuid)]])
  }

  func report(_ payload: [String: Any?]) {
    sendEvent("onAdvertisingState", payload)
  }
}

final class PeripheralDelegate: NSObject, CBPeripheralManagerDelegate {
  weak var module: TrustlaneBleModule?

  init(module: TrustlaneBleModule) {
    self.module = module
  }

  func peripheralManagerDidUpdateState(_ peripheral: CBPeripheralManager) {
    if peripheral.state == .poweredOn {
      module?.advertise()
    } else {
      module?.report(["advertising": false, "error": "Bluetooth is not available (state \(peripheral.state.rawValue))"])
    }
  }

  func peripheralManagerDidStartAdvertising(_ peripheral: CBPeripheralManager, error: Error?) {
    module?.report(["advertising": error == nil, "error": error?.localizedDescription])
  }
}
