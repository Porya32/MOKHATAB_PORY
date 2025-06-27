import os
import time
import sys

# --- 🎨 ANSI Colors ---
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RED = "\033[91m"
RESET = "\033[0m"
BOLD = "\033[1m"

# --- 🖥️ Type Animation ---
def type_out(text, delay=0.02, color=GREEN):
    for char in text:
        sys.stdout.write(f"{color}{char}{RESET}")
        sys.stdout.flush()
        time.sleep(delay)
    print()

# --- 🧱 Animated Banner PORY ---
def show_animated_banner():
    os.system('cls' if os.name == 'nt' else 'clear')
    banner_lines = [
        "██████╗  ██████╗ ██████╗ ██╗   ██╗",
        "██╔══██╗██╔═══██╗██╔══██╗╚██╗ ██╔╝",
        "██████╔╝██║   ██║██████╔╝ ╚████╔╝ ",
        "██╔═══╝ ██║   ██║██╔═══╝   ╚██╔╝  ",
        "██║     ╚██████╔╝██║        ██║   ",
        "╚═╝      ╚═════╝ ╚═╝        ╚═╝   ",
        "         P   O   R   Y           ",
    ]
    for line in banner_lines:
        type_out(line, delay=0.005, color=GREEN)
        time.sleep(0.03)
    time.sleep(0.5)
    print()

# --- 📇 Generate Contacts File ---
def generate_contacts():
    show_animated_banner()

    try:
        type_out("📦 Chand mokhatab mikhay besazam?", color=YELLOW)
        count = int(input(f"{CYAN}> {RESET}"))

        type_out("🧾 Esme mokhatab-ha chiye? (mesalan: Pory)", color=YELLOW)
        base_name = input(f"{CYAN}> {RESET}")

        type_out("📞 Shomare avvaliye ro vared kon (mesalan: 0912345)", color=YELLOW)
        base_number = input(f"{CYAN}> {RESET}")

        # 📁 ساخت پوشه PORY اگر وجود نداشت
        output_dir = "PORY"
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
            type_out(f"📂 Poushe '{output_dir}/' sakhte shod.\n", color=CYAN)
        else:
            type_out(f"📁 Poushe '{output_dir}/' az ghabl vojood dasht.\n", color=CYAN)

        filename = os.path.join(output_dir, "contacts.vcf")
        type_out(f"\n🛠 Dar hale sakhtane file {filename} ...\n", delay=0.02, color=GREEN)

        with open(filename, "w", encoding="utf-8") as file:
            for i in range(1, count + 1):
                name = f"{base_name} {i}"
                number = f"{base_number}{str(i).zfill(3)}"
                contact = f"""BEGIN:VCARD
VERSION:3.0
FN:{name}
TEL;TYPE=CELL:{number}
END:VCARD
"""
                file.write(contact + "\n")
                type_out(f"[+] Sakht shod: {name} => {number}", delay=0.005, color=CYAN)
                time.sleep(0.005)

        type_out(f"\n✅ Tamoom shod! File '{filename}' ba {count} mokhatab zakhire shod.", delay=0.02, color=GREEN)

    except KeyboardInterrupt:
        type_out("\n❌ Amaliet laghv shod tavasote karbar.", color=RED)
    except Exception as e:
        type_out(f"⚠️ Eshkal: {e}", color=RED)

# --- Run ---
if __name__ == "__main__":
    generate_contacts()