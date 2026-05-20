#!/usr/bin/env python3
"""
Tradixa Link Bridge Agent v1.0.0
================================
Aplikasi desktop bridge yang menghubungkan browser Web POS Tradixa ERP
dengan mesin EDC fisik melalui komunikasi Serial Port (USB/Bluetooth).

Arsitektur:
  [Web Browser POS] --WebSocket--> [Bridge Agent] --Serial Port--> [Mesin EDC Fisik]

Cara penggunaan:
  1. Colokkan mesin EDC ke komputer via USB atau pasangkan via Bluetooth.
  2. Jalankan aplikasi ini (klik 2x file .exe atau .app).
  3. Buka halaman kasir POS Tradixa di browser, pilih "EDC Local Bridge".
  4. Transaksi akan otomatis dikirim ke mesin EDC fisik.

Didukung oleh: pyserial + websockets
"""

import asyncio
import json
import logging
import sys
import os
import time
import threading
from datetime import datetime

import serial
import serial.tools.list_ports
import websockets

# ============================================================
#  KONFIGURASI
# ============================================================

WS_PORT = 9000                    # Port WebSocket untuk browser
BAUD_RATE = 9600                  # Kecepatan komunikasi serial standar EDC
SERIAL_TIMEOUT = 1                # Timeout baca serial (detik)
EDC_RESPONSE_TIMEOUT = 60         # Maksimum waktu tunggu respon dari EDC (detik)
LOG_FILE = "tradixa_bridge.log"   # File log untuk debugging

# ============================================================
#  SETUP LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("TradixaBridge")

# ============================================================
#  SERIAL PORT MANAGER
# ============================================================

class SerialPortManager:
    """Mengelola koneksi serial ke mesin EDC fisik."""

    def __init__(self):
        self.port: serial.Serial | None = None
        self.port_name: str = ""

    def scan_ports(self) -> list[dict]:
        """Mendeteksi semua port serial yang tersedia di komputer."""
        ports = serial.tools.list_ports.comports()
        result = []
        for p in ports:
            result.append({
                "device": p.device,
                "description": p.description,
                "hwid": p.hwid,
            })
            log.info(f"  Port ditemukan: {p.device} — {p.description}")
        if not result:
            log.warning("  Tidak ada port serial yang terdeteksi.")
        return result

    def connect(self, port_name: str | None = None) -> bool:
        """
        Membuka koneksi serial ke mesin EDC.
        Jika port_name tidak diberikan, otomatis memilih port pertama yang tersedia.
        """
        try:
            if self.port and self.port.is_open:
                self.port.close()

            if not port_name:
                ports = serial.tools.list_ports.comports()
                if not ports:
                    log.error("Tidak ada port serial yang tersedia.")
                    return False
                port_name = ports[0].device
                log.info(f"Auto-select port: {port_name}")

            self.port = serial.Serial(
                port=port_name,
                baudrate=BAUD_RATE,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=SERIAL_TIMEOUT,
            )
            self.port_name = port_name
            log.info(f"✅ Berhasil terhubung ke port serial: {port_name}")
            return True
        except serial.SerialException as e:
            log.error(f"❌ Gagal membuka port serial {port_name}: {e}")
            return False

    def disconnect(self):
        """Menutup koneksi serial."""
        if self.port and self.port.is_open:
            self.port.close()
            log.info(f"🔌 Port serial {self.port_name} ditutup.")

    def send_purchase_command(self, amount: int) -> dict:
        """
        Mengirim perintah pembelian (purchase) ke mesin EDC via serial.
        
        CATATAN PENTING:
        ================
        Protokol ECR (Electronic Cash Register) berbeda-beda untuk setiap
        vendor/bank EDC (BCA, Mandiri, BRI, Ingenico, PAX, Verifone, dll).
        
        Fungsi ini menggunakan format GENERIC yang umum digunakan.
        Anda HARUS menyesuaikan format byte/command sesuai dengan
        dokumentasi ECR dari bank/vendor EDC Anda.
        
        Contoh format umum ECR:
        - STX (0x02) + LEN + CMD + DATA + ETX (0x03) + LRC
        - Atau format JSON/ASCII tergantung vendor
        
        Args:
            amount: Nominal transaksi dalam Rupiah (integer)
        
        Returns:
            dict: { "status": "success"/"failed", "trace_number": "...", "message": "..." }
        """
        if not self.port or not self.port.is_open:
            return {
                "status": "failed",
                "message": "Port serial tidak terhubung. Silakan hubungkan mesin EDC terlebih dahulu.",
                "trace_number": "",
            }

        try:
            # ============================================================
            #  FORMAT PERINTAH ECR - SESUAIKAN DENGAN VENDOR EDC ANDA
            # ============================================================
            #
            # Contoh format generic ECR (Purchase command):
            #   STX (0x02) | Transaction Type (2 bytes) | Amount (12 bytes) | ETX (0x03) | LRC
            #
            # Transaction Type: "00" = Purchase/Sale
            # Amount: 12 digit, right-aligned, zero-padded (dalam sen/cent)
            #   Contoh: Rp 150.000 → "000000015000000" (tanpa desimal untuk IDR)
            #
            # VENDOR SPESIFIK:
            # - Ingenico: Menggunakan protokol TLV atau ECR Telium
            # - PAX: Menggunakan protokol PAX ECR Protocol
            # - Verifone: Menggunakan protokol Verifone ECR
            # - BCA/Mandiri: Biasanya menyediakan SDK/library khusus
            #
            # Silakan ganti blok di bawah ini sesuai dokumentasi ECR Anda:

            STX = b'\x02'
            ETX = b'\x03'
            transaction_type = b'00'  # 00 = Purchase/Sale
            amount_str = str(amount).zfill(12).encode('ascii')
            
            # Bangun frame perintah
            payload = transaction_type + amount_str
            lrc = 0
            for byte in payload:
                lrc ^= byte
            lrc ^= ETX[0]

            command = STX + payload + ETX + bytes([lrc])

            log.info(f"📤 Mengirim perintah purchase ke EDC: Rp {amount:,}")
            log.info(f"   Raw bytes: {command.hex()}")

            # Kirim ke mesin EDC
            self.port.reset_input_buffer()
            self.port.write(command)
            self.port.flush()

            # ============================================================
            #  TUNGGU RESPON DARI MESIN EDC
            # ============================================================
            # Pada titik ini, mesin EDC akan:
            # 1. Menampilkan layar "Gesek/Tap/Insert Kartu"
            # 2. Pelanggan memasukkan kartu dan PIN
            # 3. EDC menghubungi bank untuk otorisasi
            # 4. EDC mengirim respon balik via serial

            log.info("⏳ Menunggu pelanggan gesek/tap kartu di mesin EDC...")

            response_data = b''
            start_time = time.time()

            while time.time() - start_time < EDC_RESPONSE_TIMEOUT:
                if self.port.in_waiting > 0:
                    chunk = self.port.read(self.port.in_waiting)
                    response_data += chunk

                    # Cek apakah respon sudah lengkap (diakhiri ETX)
                    if ETX in response_data:
                        break
                time.sleep(0.1)

            if not response_data:
                return {
                    "status": "failed",
                    "message": f"Timeout: Mesin EDC tidak merespon dalam {EDC_RESPONSE_TIMEOUT} detik.",
                    "trace_number": "",
                }

            log.info(f"📥 Respon diterima dari EDC: {response_data.hex()}")

            # ============================================================
            #  PARSE RESPON ECR - SESUAIKAN DENGAN VENDOR EDC ANDA
            # ============================================================
            # Format respon tiap vendor berbeda-beda.
            # Contoh generic parsing:
            #   Byte 0: STX (0x02)
            #   Byte 1-2: Response Code ("00" = Approved, "05" = Declined, dll)
            #   Byte 3-8: Trace Number (6 digit)
            #   ...
            #   Byte N: ETX (0x03)
            #   Byte N+1: LRC

            parsed = self._parse_ecr_response(response_data)
            return parsed

        except serial.SerialException as e:
            log.error(f"❌ Error komunikasi serial: {e}")
            return {
                "status": "failed",
                "message": f"Error komunikasi serial: {str(e)}",
                "trace_number": "",
            }

    def _parse_ecr_response(self, raw: bytes) -> dict:
        """
        Mem-parsing respon byte dari mesin EDC.
        
        PENTING: Sesuaikan parsing ini dengan format respon dari
        vendor/bank EDC Anda. Contoh di bawah adalah format generic.
        """
        try:
            # Hapus STX dan ETX
            payload = raw.strip(b'\x02\x03')
            
            # Ambil response code (2 byte pertama)
            if len(payload) >= 2:
                response_code = payload[0:2].decode('ascii', errors='ignore')
            else:
                response_code = "99"

            # Ambil trace number (6 byte berikutnya)
            if len(payload) >= 8:
                trace_number = payload[2:8].decode('ascii', errors='ignore')
            else:
                trace_number = str(int(time.time()))[-6:]

            # Response code mapping (standar ISO 8583)
            approved_codes = ["00", "08", "10", "11", "85"]

            if response_code in approved_codes:
                log.info(f"✅ Transaksi DISETUJUI! Response Code: {response_code}, Trace: {trace_number}")
                return {
                    "status": "success",
                    "trace_number": trace_number,
                    "message": "Transaksi Disetujui",
                    "response_code": response_code,
                }
            else:
                reason = self._get_decline_reason(response_code)
                log.warning(f"❌ Transaksi DITOLAK. Response Code: {response_code} — {reason}")
                return {
                    "status": "failed",
                    "trace_number": trace_number,
                    "message": f"Ditolak: {reason} (Kode: {response_code})",
                    "response_code": response_code,
                }
        except Exception as e:
            log.error(f"Error parsing respon EDC: {e}")
            return {
                "status": "failed",
                "message": f"Gagal membaca respon dari mesin EDC: {str(e)}",
                "trace_number": "",
            }

    @staticmethod
    def _get_decline_reason(code: str) -> str:
        """Mengubah response code menjadi alasan penolakan yang mudah dipahami."""
        reasons = {
            "01": "Hubungi bank penerbit kartu",
            "03": "Merchant tidak valid",
            "04": "Kartu ditahan oleh mesin",
            "05": "Transaksi ditolak",
            "12": "Transaksi tidak valid",
            "13": "Nominal tidak valid",
            "14": "Nomor kartu tidak valid",
            "30": "Format error",
            "41": "Kartu hilang - ditahan",
            "43": "Kartu dicuri - ditahan",
            "51": "Saldo tidak mencukupi",
            "54": "Kartu kedaluwarsa",
            "55": "PIN salah",
            "61": "Melebihi batas transaksi",
            "65": "Melebihi frekuensi transaksi",
            "75": "PIN salah berulang kali",
            "91": "Bank penerbit tidak dapat dihubungi",
            "96": "System malfunction",
        }
        return reasons.get(code, "Alasan tidak diketahui")


# ============================================================
#  WEBSOCKET SERVER (Komunikasi dengan Browser POS)
# ============================================================

serial_manager = SerialPortManager()

async def handle_websocket(websocket):
    """Menangani koneksi WebSocket dari browser Web POS Tradixa."""
    client_ip = websocket.remote_address
    log.info(f"🌐 Koneksi WebSocket baru dari: {client_ip}")

    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                command = data.get("command", "")
                log.info(f"📨 Perintah diterima: {data}")

                if command == "purchase":
                    amount = int(data.get("amount", 0))
                    if amount <= 0:
                        await websocket.send(json.dumps({
                            "status": "failed",
                            "message": "Nominal transaksi tidak valid (harus > 0).",
                        }))
                        continue

                    # Coba koneksi otomatis ke EDC jika belum terhubung
                    if not serial_manager.port or not serial_manager.port.is_open:
                        log.info("🔍 Mencari mesin EDC yang terhubung...")
                        if not serial_manager.connect():
                            await websocket.send(json.dumps({
                                "status": "failed",
                                "message": "Tidak ada mesin EDC yang terdeteksi. "
                                           "Pastikan mesin EDC sudah terhubung via USB atau Bluetooth.",
                            }))
                            continue

                    # Kirim perintah ke EDC (proses di thread terpisah agar tidak block)
                    result = await asyncio.get_event_loop().run_in_executor(
                        None, serial_manager.send_purchase_command, amount
                    )

                    await websocket.send(json.dumps(result))

                elif command == "scan_ports":
                    ports = serial_manager.scan_ports()
                    await websocket.send(json.dumps({
                        "status": "success",
                        "ports": ports,
                    }))

                elif command == "connect":
                    port_name = data.get("port", None)
                    success = serial_manager.connect(port_name)
                    await websocket.send(json.dumps({
                        "status": "success" if success else "failed",
                        "message": f"Terhubung ke {serial_manager.port_name}" if success else "Gagal terhubung.",
                    }))

                elif command == "ping":
                    await websocket.send(json.dumps({
                        "status": "success",
                        "message": "Tradixa Link Bridge aktif.",
                        "version": "1.0.0",
                        "serial_connected": bool(serial_manager.port and serial_manager.port.is_open),
                        "serial_port": serial_manager.port_name if serial_manager.port else "",
                    }))

                else:
                    await websocket.send(json.dumps({
                        "status": "failed",
                        "message": f"Perintah tidak dikenali: {command}",
                    }))

            except json.JSONDecodeError:
                await websocket.send(json.dumps({
                    "status": "failed",
                    "message": "Format data JSON tidak valid.",
                }))
            except Exception as e:
                log.error(f"Error memproses perintah: {e}")
                await websocket.send(json.dumps({
                    "status": "failed",
                    "message": f"Error internal: {str(e)}",
                }))

    except websockets.exceptions.ConnectionClosed:
        log.info(f"🔌 Koneksi WebSocket dari {client_ip} ditutup.")


# ============================================================
#  MAIN ENTRY POINT
# ============================================================

async def main():
    """Titik masuk utama aplikasi Tradixa Link Bridge."""

    print()
    print("=" * 60)
    print("  Tradixa Link Bridge Agent v1.0.0")
    print("  EDC Serial ↔ WebSocket Bridge")
    print("=" * 60)
    print()
    print(f"  WebSocket Server  : ws://localhost:{WS_PORT}")
    print(f"  Baud Rate         : {BAUD_RATE}")
    print(f"  Log File          : {LOG_FILE}")
    print()

    # Scan port serial yang tersedia
    print("  Scanning port serial...")
    ports = serial_manager.scan_ports()
    if ports:
        print(f"  ✅ Ditemukan {len(ports)} port serial:")
        for p in ports:
            print(f"     - {p['device']}: {p['description']}")

        # Auto-connect ke port pertama
        serial_manager.connect()
    else:
        print("  ⚠️  Tidak ada mesin EDC terdeteksi.")
        print("     Colokkan mesin EDC via USB/Bluetooth,")
        print("     lalu restart aplikasi ini.")
        print("     (Bridge tetap aktif dan siap menerima koneksi)")

    print()
    print("  ⏳ Menunggu koneksi dari Web POS Tradixa...")
    print("  (Jangan tutup jendela ini saat kasir sedang beroperasi)")
    print()

    # Start WebSocket server
    async with websockets.serve(handle_websocket, "localhost", WS_PORT):
        await asyncio.Future()  # Jalan selamanya


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bridge dihentikan oleh pengguna.")
        serial_manager.disconnect()
    except Exception as e:
        log.error(f"Fatal error: {e}")
        serial_manager.disconnect()
        sys.exit(1)
