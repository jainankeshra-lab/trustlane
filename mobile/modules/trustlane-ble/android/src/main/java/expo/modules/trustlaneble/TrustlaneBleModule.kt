package expo.modules.trustlaneble

import android.annotation.SuppressLint
import android.bluetooth.BluetoothManager
import android.bluetooth.le.AdvertiseCallback
import android.bluetooth.le.AdvertiseData
import android.bluetooth.le.AdvertiseSettings
import android.bluetooth.le.BluetoothLeAdvertiser
import android.content.Context
import android.os.ParcelUuid
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import java.util.UUID

// Advertises one 128-bit service UUID that carries the trip's rotating Bluetooth ID.
// JS requests BLUETOOTH_ADVERTISE at runtime before calling startAdvertising (Android 12+).
@SuppressLint("MissingPermission")
class TrustlaneBleModule : Module() {
  private var callback: AdvertiseCallback? = null

  private val advertiser: BluetoothLeAdvertiser?
    get() = (appContext.reactContext?.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)
      ?.adapter?.bluetoothLeAdvertiser

  override fun definition() = ModuleDefinition {
    Name("TrustlaneBle")

    Events("onAdvertisingState")

    Function("isSupported") {
      return@Function advertiser != null
    }

    Function("startAdvertising") { serviceUuid: String ->
      stop()
      val adv = advertiser ?: throw IllegalStateException("Bluetooth LE advertising is not available on this phone")
      val settings = AdvertiseSettings.Builder()
        .setAdvertiseMode(AdvertiseSettings.ADVERTISE_MODE_LOW_LATENCY)
        .setTxPowerLevel(AdvertiseSettings.ADVERTISE_TX_POWER_MEDIUM)
        .setConnectable(false)
        .build()
      val data = AdvertiseData.Builder()
        .setIncludeDeviceName(false)
        .addServiceUuid(ParcelUuid(UUID.fromString(serviceUuid)))
        .build()
      val cb = object : AdvertiseCallback() {
        override fun onStartSuccess(settingsInEffect: AdvertiseSettings?) {
          sendEvent("onAdvertisingState", mapOf("advertising" to true, "uuid" to serviceUuid))
        }

        override fun onStartFailure(errorCode: Int) {
          sendEvent("onAdvertisingState", mapOf("advertising" to false, "error" to errorCode))
        }
      }
      callback = cb
      adv.startAdvertising(settings, data, cb)
    }

    Function("stopAdvertising") {
      stop()
    }

    OnDestroy {
      stop()
    }
  }

  private fun stop() {
    val cb = callback ?: return
    try {
      advertiser?.stopAdvertising(cb)
    } catch (_: SecurityException) {
      // permission revoked while advertising: nothing left to stop
    }
    callback = null
  }
}
