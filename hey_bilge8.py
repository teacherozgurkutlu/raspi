import os
import sys
import platform
import tempfile

WINDOWS = platform.system() == "Windows"
METIN_MODU = "--metin" in sys.argv   # mikrofon yerine klavyeden komut yaz

# Pi'de EMA modelleri onceden indirilmis: her acilista internete
# baglanip kontrol etmesin (hey_bilge5'teki ayar).
if not WINDOWS:
    os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

import json
import random
import datetime
import re
from difflib import SequenceMatcher
import time
import queue
import threading
import subprocess
from collections import deque

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

# ---- Konusma tanima: Vosk (Turkce) ----
try:
    from vosk import Model as VoskModel, KaldiRecognizer, SetLogLevel
    SetLogLevel(-1)   # Vosk'un ayrintili loglarini kapat
except ImportError:
    VoskModel = None

# ---- GPIO: Pi'de gercek LED, Windows'ta simulasyon ----
try:
    from gpiozero import LED
    if WINDOWS:
        raise ImportError
except Exception:
    class LED:
        def __init__(self, pin):
            self.pin = pin
        def on(self):
            print(f"[SIMULASYON] LED (pin {self.pin}) YANDI")
        def off(self):
            print(f"[SIMULASYON] LED (pin {self.pin}) SONDU")
        def close(self):
            pass

# ---- TTS: EMA Lightning, yoksa Windows'un kendi sesi ----
try:
    from ema_lightning import EMA
except ImportError:
    EMA = None


# ==========================================
# AYARLAR
# ==========================================

# Vosk Turkce modeli.
# Bu klasor dosyanin yaninda varsa oradan yuklenir; yoksa ilk calistirmada
# internetten otomatik indirilir (~35 MB, kullanicinin .cache klasorune).
BETIK_KLASORU = os.path.dirname(os.path.abspath(__file__))
VOSK_MODEL_YOLU = os.path.join(BETIK_KLASORU, "vosk-model-small-tr-0.3")

# True: Vosk sadece asagidaki komut cumlelerini tanir (cok daha isabetli).
# False: serbest konusma tanir, karar bulanik eslestirmeye kalir.
GRAMER_MODU = True

# Pi'de USB mikrofon 1 numarali cihaz; Windows'ta varsayilan mikrofon
MIC_DEVICE = None if WINDOWS else 1
MIC_RATE = 44100
ASR_RATE = 16000

BLOCK_MS = 100

VOLUME_THRESHOLD = 650
# "Merhaba Defne ... komut" arasindaki kisa duraksamada kayit bolunmesin
SILENCE_SECONDS = 1.0
MIN_SPEECH_SECONDS = 0.3
MAX_SPEECH_SECONDS = 6.0

WAKE_TIMEOUT = 12.0

# Anlasilmayan komutta en fazla kac kez "tekrar soyler misin" diye sorsun.
# Limit dolunca uyku moduna doner, yeniden "Merhaba Defne" beklenir.
MAX_TEKRAR = 2

# Uyandirma ifadeleri (Turkce karakterlerle yaz)
UYANDIRMA = [
    "merhaba defne",
    "hey defne",
]

LED_PIN = 17

TTS_SPEED = 0.8
TTS_SEED = 200
TTS_FILE = os.path.join(tempfile.gettempdir(), "defne_ema.wav")

# Seslendirme bitince beklenecek sure
TTS_COOLDOWN = 0.8

# Bulanik eslestirme esikleri (0.0 - 1.0)
# Yukseltirsen daha secici olur, dusurursen daha toleransli.
COMMAND_THRESHOLD = 0.80
WAKE_THRESHOLD = 0.75
# En iyi iki aday farkli komutlarsa ve skorlari bu kadar yakinsa
# (orn. "turn on" / "turn off") emin olmadigi icin reddeder.
AMBIGUITY_MARGIN = 0.02

# Komut cumleleri (Turkce karakterlerle, kucuk harfle yaz).
# Not: "lambayı" ve "söndür" Vosk'un kucuk Turkce modelinin sozlugunde yok,
# bu yuzden kullanilmiyor. Acilista sozlukte olmayan kelime uyarisi
# cikarsa o cumleyi baska bir soyleyisle degistir.
# Yeni bir soyleyis eklemek icin buraya bir satir eklemen yeterli.
KOMUTLAR = {
    "ışığı aç": "LIGHT_ON",
    "ışıkları aç": "LIGHT_ON",
    "ışığı yak": "LIGHT_ON",
    "ışıkları yak": "LIGHT_ON",
    "ışık aç": "LIGHT_ON",
    "lamba aç": "LIGHT_ON",
    "lambaları aç": "LIGHT_ON",
    "ışığı açar mısın": "LIGHT_ON",
    "ışıkları açar mısın": "LIGHT_ON",

    "ışığı kapat": "LIGHT_OFF",
    "ışıkları kapat": "LIGHT_OFF",
    "ışık kapat": "LIGHT_OFF",
    "lamba kapat": "LIGHT_OFF",
    "lambaları kapat": "LIGHT_OFF",
    "ışığı kapatır mısın": "LIGHT_OFF",
    "ışıkları kapatır mısın": "LIGHT_OFF",

    "sıcaklık kaç": "TEMPERATURE",
    "sıcaklık kaç derece": "TEMPERATURE",
    "sıcaklık nedir": "TEMPERATURE",
    "sıcaklığı söyle": "TEMPERATURE",
    "hava kaç derece": "TEMPERATURE",
    "oda kaç derece": "TEMPERATURE",
    "kaç derece": "TEMPERATURE",

    "sınıf listesi": "CLASS",
    "sınıf listesini oku": "CLASS",
    "yoklama": "CLASS",
    "yoklama al": "CLASS",

    "saat kaç": "TIME",
    "saati söyle": "TIME",
    "şu an saat kaç": "TIME",

    "bugün günlerden ne": "DATE",
    "bugün hangi gün": "DATE",
    "bugün ayın kaçı": "DATE",
    "tarih ne": "DATE",
    "bugünün tarihi": "DATE",

    "basınç kaç": "PRESSURE",
    "hava basıncı kaç": "PRESSURE",
    "basınç nedir": "PRESSURE",
    "basıncı söyle": "PRESSURE",

    "rastgele öğrenci seç": "RANDOM_STUDENT",
    "bir öğrenci seç": "RANDOM_STUDENT",
    "öğrenci seç": "RANDOM_STUDENT",

    "neler yapabilirsin": "HELP",
    "ne yapabilirsin": "HELP",
    "komutları say": "HELP",
    "yardım": "HELP",

    "tekrar et": "REPEAT",
    "tekrar söyle": "REPEAT",
    "bir daha söyle": "REPEAT",
    "ne dedin": "REPEAT",

    "sayaç kur": "TIMER",
    # Not: "sayacı" Vosk sozlugunde yok
    "sayaç iptal": "TIMER_CANCEL",
    "sayaç iptal et": "TIMER_CANCEL",
    "iptal et": "TIMER_CANCEL",
    "sayaç dursun": "TIMER_CANCEL",
    "sayaç ne kadar kaldı": "TIMER_LEFT",
    "kalan süre": "TIMER_LEFT",
    "ne kadar kaldı": "TIMER_LEFT",

    "beni güldür": "JOKE",
    "espri yap": "JOKE",
    "bir espri yap": "JOKE",
    "espri anlat": "JOKE",
    "şaka yap": "JOKE",
    "fıkra anlat": "JOKE",

    "dinlemeyi durdur": "STOP",
    "dinlemeyi bitir": "STOP",
    "dur": "STOP",
    "sus": "STOP",

    # "Açayım mı, kapatayım mı?" sorusuna kisa cevaplar
    "aç": "LIGHT_ON",
    "yak": "LIGHT_ON",
    "kapat": "LIGHT_OFF",
}

# Sinif listesi ("sinif listesi" ve "rastgele ogrenci sec" komutlari)
OGRENCILER = [
    "Göktuğ Karaca",
    "Kerem Karakuzu",
    "Kerem Sular",
    "Ali Mert Kaptan",
    "Berat Yaşar",
    "Haktan Ata Sarı",
    "Mustafa Kemal Tunalı",
    "Ömer Asaf İnceoğlu",
    "Demir Bozdemir",
    "Bartu Durmuş",
    "Burak Gündüz",
    "İrem Afacan",
    "İdil Demiryılmaz",
    "Melek Aktan",
    "Mehmet Şahan",
    "Ege Türkmen",
    "Eymen Çetinkaya",
    "Alp Deniz Buharalıoğlu",
    "Çağan Çavdar",
    "Eyüp Emir Akgüngör",
    "Serkan Semerci",
    "Yusuf Bera Mutlu",
    "Ahmet Eren Demiralay",
    "Musa Oğuz Özyücel",
    "Yusuf Sacit Acer",
    "Ada Çolban",
    "Eylül Akyüz",
    "Öykü Aydın",
    "Zeynep Sıla Akyüz",
    "İsmail Metehan Şahin",
    "Ömer Çalık",
    "Pelinsu Ok",
    "Kadriye Beril İktuğ",
    "Nurşat Emir Civan",
    "Alp Somuncuoğlu",
    "Ela Taşan",
    "Eren Zengül",
    "Hasan Kırca",
    "Rüzgar Dağaşan",
    "Selen Özer",
    "Ömer Erkek",
    "Erol Işık",
    "Eslem Yılmaz",
    "Nehir Diken",
    "Ümit Demir Efe",
    "Ahmet Talha Baştimur",
    "Kerem Yılmaz",
    "İsa Furkan Işık",
]

# Ogrenci adi soylenince verilecek cevap ({isim} yerine ogrencinin adi gelir)
OGRENCI_CEVABI = "{isim}, seni kaydettim. Artık gözüm üstünde!"
# Kayitlar bu dosyaya eklenir (tarih, saat, ogrenci). Excel ile acilabilir.
OGRENCI_KAYIT_DOSYASI = "ogrenci_kayitlari.csv"
# Ogrenci adi icin bulanik eslestirme esigi
OGRENCI_THRESHOLD = 0.85

# "Beni guldur" komutu icin soguk espriler (rastgele, tekrarsiz secilir)
SOGUK_ESPRILER = [
    "Adamın biri gülmüş, saksıya dikmişler.",
    "Kalemin ucu kırılmış. Kalem de ucuz atlattım demiş.",
    "En çok ders çalışan hayvan hangisidir? Kitap kurdu.",
    "Örümcek adam evine nasıl girer? Ağ geçidinden.",
    "Matematik kitabı neden üzgünmüş? Çünkü çok problemi varmış.",
    "Denize kırmızı taş atarsan ne olur? Islanır.",
    "Bilgisayar neden üşümüş? Çünkü pencereyi açık bırakmışlar.",
    "Saat neden okula gitmiş? Çünkü geri kalmış.",
    "Hangi kuş hiç konmaz? Kuşkonmaz.",
    "Hangi derste hiç ödev verilmez? Teneffüste.",
    "Yumurta neden okula gitmemiş? Çünkü kırılmaktan korkmuş.",
    "Cetvel neden hiç yalan söylemez? Çünkü hep doğruyu gösterir.",
    "Kalorifer neden bu kadar sevilir? Çünkü herkes ona ısınmış.",
    "Buzdolabı neden hiç sinirlenmez? Çünkü çok soğukkanlıdır.",
    "Lamba neden derste parmak kaldırmış? Çünkü parlak bir fikri varmış.",
    "Sıfır sekize ne demiş? Kemerin çok yakışmış.",
    "Dişçi işini neden çok sever? Çünkü işi dişe dokunur.",
    "Fırıncı neden hiç işsiz kalmaz? Çünkü her gün ekmeğini kazanır.",
    "Ördek neden öğretmen olmuş? Çünkü vak vak, vakti varmış.",
    "Pil neden hep mutluymuş? Çünkü dolu dolu yaşıyormuş.",
    "Sandalye neden yorgunmuş? Çünkü bütün gün üstüne oturulmuş.",
    "Ağaç derste neden dalgınmış? Çünkü dalları varmış.",
    "Ayakkabılar neden küsmüş? Çünkü teki tekine uymamış.",
    "Kardan adam neden sınıfta kalmış? Çünkü sınavda donup kalmış.",
    "Arı neden okula gitmiş? Çünkü bal gibi öğrenmek istemiş.",
]

# Sayac: Vosk'un taniyacagi sayilar ve cumle kaliplari.
# "{n}" yerine asagidaki sayilar konur: "beş dakika sayaç kur" gibi.
SAYAC_SAYILARI = [
    "bir", "iki", "üç", "dört", "beş", "altı", "yedi", "sekiz", "dokuz",
    "on", "on beş", "yirmi", "yirmi beş", "otuz", "kırk", "kırk beş",
    "elli", "altmış",
]
SAYAC_KALIPLARI = [
    "{n} dakika sayaç kur",
    "{n} dakikalık sayaç kur",
    "{n} saniye sayaç kur",
]
SAYAC_CUMLELERI = [k.format(n=n) for k in SAYAC_KALIPLARI for n in SAYAC_SAYILARI]

# Asagidaki listeler Turkce karakterleri sadelestirilmis haldedir
# (ı->i, ş->s, ğ->g, ü->u, ö->o, ç->c); eslestirme bu sekilde yapilir.
# Bu kelimelerden biri geciyor ama ac/kapat duyulmadiysa
# asistan "acayim mi kapatayim mi?" diye sorar.
LIGHT_WORDS = ("isik", "isigi", "isiklar", "isiklari",
               "lamba", "lambayi", "lambalari", "led")
ON_WORDS = ("ac", "acar", "acin", "acsana", "acabilir",
            "yak", "yakar", "yakin", "yaksana")
OFF_WORDS = ("kapat", "kapa", "kapatir", "kapatin", "kapatsana",
             "kapatabilir", "sondur", "sondurur")

# ==========================================
# GPIO
# ==========================================

led = LED(LED_PIN)
led.off()
# ==========================================
# KUYRUKLAR VE DURUMLAR
# ==========================================

audio_queue = queue.Queue(maxsize=120)
speech_queue = queue.Queue(maxsize=5)

tts_active = threading.Event()
stop_event = threading.Event()

mute_until = 0.0
audio_overflows = 0

state = "SLEEP"
wake_deadline = 0.0
deneme_sayisi = 0


# ==========================================
# EMA LIGHTNING
# ==========================================

if EMA is not None:
    print("EMA Lightning yukleniyor...")
    tts = EMA()
    print("EMA Lightning hazir.")
else:
    tts = None
    print("EMA Lightning yok -> sistem sesi kullanilacak.")


def windows_seslendir(metin):
    """Windows'un yerlesik konusma motoruyla okur (ek paket gerekmez).
    Turkce ses yukluyse onu secer."""
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$tr = $s.GetInstalledVoices() | Where-Object "
        "{ $_.VoiceInfo.Culture.Name -eq 'tr-TR' } | Select-Object -First 1;"
        "if ($tr) { $s.SelectVoice($tr.VoiceInfo.Name) };"
        "$s.Speak($env:ASISTAN_METIN)"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        env={**os.environ, "ASISTAN_METIN": metin},
        check=True
    )


def wav_cal(yol):
    if WINDOWS:
        import winsound
        winsound.PlaySound(yol, winsound.SND_FILENAME)
    else:
        subprocess.run(["pw-play", yol], check=True)


def mikrofon_sessiz():
    return (
        tts_active.is_set()
        or time.monotonic() < mute_until
    )


def kuyrugu_temizle(q):
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            break


konus_kilidi = threading.Lock()   # sayac ile asistan ayni anda konusmasin
son_cevap = None                   # "tekrar et" komutu icin


def konus(metin, kaydet=True):
    global son_cevap

    if kaydet:
        son_cevap = metin

    with konus_kilidi:
        _konus(metin)


def _konus(metin):
    global mute_until

    print("\nDEFNE:", metin)

    # Kendi sesimizi tekrar tanimayalim
    tts_active.set()

    try:
        # Birikmis eski mikrofon verilerini temizle
        kuyrugu_temizle(audio_queue)

        if tts is not None:
            speech = tts.say(
                metin,
                speed=TTS_SPEED,
                seed=TTS_SEED,
                path=TTS_FILE
            )

            print(
                f"Ses uzunlugu: "
                f"{speech.duration:.2f} saniye"
            )

            wav_cal(TTS_FILE)

        elif WINDOWS:
            windows_seslendir(metin)

    except Exception as error:
        print("Seslendirme hatasi:", error)

    finally:
        mute_until = (
            time.monotonic() + TTS_COOLDOWN
        )

        kuyrugu_temizle(audio_queue)
        tts_active.clear()

# ==========================================
# METIN DUZENLEME
# ==========================================

_TR_SADE = str.maketrans("ışğüöçâîû", "isguocaiu")


def normalize(text):
    # Turkce buyuk/kucuk harf: I->ı, İ->i
    text = text.replace("I", "ı").replace("İ", "i").lower()
    # Eslestirmede Turkce karakter farklarini yok say
    text = text.translate(_TR_SADE)

    text = re.sub(
        r"[^\w\s]",
        " ",
        text
    )

    return " ".join(text.split())


COMMANDS = {normalize(k): v for k, v in KOMUTLAR.items()}
WAKE_PHRASES = [normalize(p) for p in UYANDIRMA]


def tr_kucuk(text):
    return text.replace("I", "ı").replace("İ", "i").lower()


# Soylenis (sadelestirilmis) -> ogrencinin tam adi.
# Baslangicta tam adlar; Vosk yuklenince sozlukte olmayan soyadlar icin
# kisaltilmis soyleyisler eklenir (bkz. ogrenci_cumlelerini_hazirla).
OGRENCI_ANAHTARLARI = {normalize(o): o for o in OGRENCILER}


def ogrenci_bul(text, bulanik=True):
    """Metinde bir ogrencinin adi geciyorsa (tam_ad, skor) dondurur."""
    text = normalize(text)
    if not text:
        return None, 0.0

    # Birebir
    if text in OGRENCI_ANAHTARLARI:
        return OGRENCI_ANAHTARLARI[text], 1.0

    # Cumlenin icinde geciyorsa (en uzun ad once)
    padded = f" {text} "
    for anahtar in sorted(OGRENCI_ANAHTARLARI, key=len, reverse=True):
        if len(anahtar) >= 5 and f" {anahtar} " in padded:
            return OGRENCI_ANAHTARLARI[anahtar], 0.99

    if not bulanik or len(text) < 6:
        return None, 0.0

    # Bulanik: her ogrencinin en iyi skoru alinir (bir ogrencinin birden
    # fazla soylenisi olabilir); en iyi ogrenci digerlerinden belirgin
    # sekilde iyi olmali.
    ogrenci_skoru = {}
    for anahtar, ogrenci in OGRENCI_ANAHTARLARI.items():
        skor = benzerlik(text, anahtar)
        if skor > ogrenci_skoru.get(ogrenci, 0.0):
            ogrenci_skoru[ogrenci] = skor

    skorlar = sorted(((v, k) for k, v in ogrenci_skoru.items()), reverse=True)
    en_iyi, ogrenci = skorlar[0]
    ikinci = skorlar[1][0] if len(skorlar) > 1 else 0.0

    if en_iyi >= OGRENCI_THRESHOLD and en_iyi - ikinci >= 0.05:
        return ogrenci, en_iyi

    return None, en_iyi


def benzerlik(a, b):
    return SequenceMatcher(None, a, b).ratio()


def match_command(text):
    """Yaziya hatali cevrilmis metni en yakin komuta eslestirir.
    Donus: (komut_kodu veya None, eslesen_ifade, skor)"""
    text = normalize(text)

    if not text:
        return None, None, 0.0

    # 1) Birebir eslesme
    if text in COMMANDS:
        return COMMANDS[text], text, 1.0

    # 1b) Ogrenci adi (Erol Isik gibi adlar isik komutuna karismasin diye
    #     isik kuralindan once bakilir)
    isim, skor = ogrenci_bul(text, bulanik=False)
    if isim:
        return "STUDENT", isim, skor

    # 2) Komut ifadesi cumlenin icinde geciyorsa
    #    (orn. "please turn on the light now")
    padded = f" {text} "
    for phrase in sorted(COMMANDS, key=len, reverse=True):
        if len(phrase) >= 6 and f" {phrase} " in padded:
            return COMMANDS[phrase], phrase, 0.99

    words = text.split()

    # 3) Sayac komutlari (sayi icerdigi icin listeyle degil kuralla)
    if any(w.startswith("sayac") or w in SURE_BIRIMLERI for w in words):
        if "iptal" in words or "durdur" in words or "dursun" in words:
            return "TIMER_CANCEL", "(sayac iptal)", 0.95
        if "kaldi" in words or "kalan" in words:
            return "TIMER_LEFT", "(sayac kalan)", 0.95
        return "TIMER", "(sayac kur)", 0.95

    # 4) Isikla ilgili cumle: on/off kelimesine gore karar ver
    if any(w in LIGHT_WORDS for w in words):
        has_on = any(w in ON_WORDS for w in words)
        has_off = any(w in OFF_WORDS for w in words)
        if has_on and not has_off:
            return "LIGHT_ON", "(light + on)", 0.95
        if has_off and not has_on:
            return "LIGHT_OFF", "(light + off)", 0.95
        return "LIGHT_ASK", "(light, on/off belirsiz)", 0.0

    # 5) Bulanik eslestirme
    scored = sorted(
        ((benzerlik(text, phrase), phrase) for phrase in COMMANDS),
        reverse=True
    )

    best_score, best_phrase = scored[0]
    best_cmd = COMMANDS[best_phrase]

    if best_score < COMMAND_THRESHOLD:
        # Komuta benzemiyorsa bir ogrenci adina benziyor mu?
        isim, skor = ogrenci_bul(text)
        if isim:
            return "STUDENT", isim, skor
        return None, best_phrase, best_score

    # Farkli bir komut neredeyse ayni skoru aldiysa belirsizdir
    for score, phrase in scored[1:]:
        if COMMANDS[phrase] != best_cmd:
            if best_score - score < AMBIGUITY_MARGIN:
                print(
                    f"Belirsiz: '{best_phrase}' ({best_score:.2f}) / "
                    f"'{phrase}' ({score:.2f})"
                )
                return None, best_phrase, best_score
            break

    # Isik komutlarinda "on" / "off" duyulmadiysa tahmin etme
    if best_cmd in ("LIGHT_ON", "LIGHT_OFF"):
        words = text.split()
        has_on = any(w in ON_WORDS for w in words)
        has_off = any(w in OFF_WORDS for w in words)

        if (
            (best_cmd == "LIGHT_ON" and (not has_on or has_off))
            or (best_cmd == "LIGHT_OFF" and not has_off)
        ):
            print(f"Belirsiz: '{text}' icinde on/off net degil")
            return None, best_phrase, best_score

    return best_cmd, best_phrase, best_score


# ==========================================
# UYANDIRMA KELIMESI
# ==========================================

def _wake_prefix(words):
    """Cumlenin basi uyandirma ifadesine benziyorsa
    kalan kelimeleri dondurur, benzemiyorsa None."""
    best = None

    for phrase in WAKE_PHRASES:
        n = len(phrase.split())

        # Ifade bazen bir kelime eksik/fazla duyulabilir
        for k in (n, n - 1, n + 1):
            # Tek kelime ("merhaba") uyandirmasin
            if k < min(2, n) or k > len(words):
                continue

            head = " ".join(words[:k])
            score = benzerlik(head, phrase)

            if score >= WAKE_THRESHOLD and (
                best is None or score > best[0]
            ):
                best = (score, words[k:])

    return None if best is None else best[1]


def find_wake(text):
    words = normalize(text).split()
    found = False

    # "merhaba defne merhaba defne ..." tekrarlarini da temizle
    while words:
        rest = _wake_prefix(words)
        if rest is None:
            break
        found = True
        words = rest

    return " ".join(words) if found else None


# ==========================================
# KOMUTLAR
# ==========================================

BMP280_YOLU = "/home/pi/sesli_asistan/bmp280.py"


def bmp280_oku():
    """BMP280 betigini calistirir, olcum sozlugunu dondurur (hata: None)."""
    if not os.path.exists(BMP280_YOLU):
        # Windows testi: sensor yok, ornek degerler
        print("[SIMULASYON] BMP280 yok, ornek degerler kullaniliyor")
        return {"ok": True, "temperature_c": 23.5, "pressure_hpa": 1013.2}

    try:
        result = subprocess.run(
            [sys.executable, BMP280_YOLU],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode != 0:
            print("BMP280 hata:", result.stderr)
            return None

        data = json.loads(result.stdout.strip())

        if not data.get("ok"):
            print("BMP280 hata:", data.get("errors"))
            return None

        return data

    except Exception as error:
        print("BMP280 okuma hatasi:", error)
        return None


def sicaklik_oku():
    data = bmp280_oku()
    if data is None or data.get("temperature_c") is None:
        return None
    return float(data["temperature_c"])


def basinc_oku():
    """Basinci hPa olarak dondurur. bmp280.py ciktisindaki anahtar adi
    bilinmedigi icin adinda 'press' gecen ilk anahtari kullanir."""
    data = bmp280_oku()
    if data is None:
        return None

    for anahtar, deger in data.items():
        if "press" in anahtar.lower() and deger is not None:
            deger = float(deger)
            # Pascal olarak geldiyse hPa'ya cevir
            if deger > 2000:
                deger /= 100.0
            return deger

    print("BMP280 ciktisinda basinc yok, gelen anahtarlar:", list(data))
    return None


def ondalikli_soyle(deger):
    """23.5 -> '23 virgül 5', 23.0 -> '23'"""
    deger = round(deger, 1)
    tam = int(deger)
    ondalik = int(round((deger - tam) * 10))
    return f"{tam}" if ondalik == 0 else f"{tam} virgül {ondalik}"


def tekrar_sor(mesaj):
    """Anlasilmayan komutta tekrar ister; MAX_TEKRAR asilinca vazgecer."""
    global state, wake_deadline, deneme_sayisi

    deneme_sayisi += 1

    if deneme_sayisi > MAX_TEKRAR:
        deneme_sayisi = 0
        konus("Anlayamadım. Tekrar denemek için Merhaba Defne de.", kaydet=False)
        state = "SLEEP"
        print("Tekrar limiti doldu. Merhaba Defne bekleniyor...")
        return

    konus(mesaj, kaydet=False)
    state = "COMMAND"
    wake_deadline = time.monotonic() + WAKE_TIMEOUT
    print(f"Tekrar istendi ({deneme_sayisi}/{MAX_TEKRAR})")


# ==========================================
# KOMUT FONKSIYONLARI
# ==========================================
# Yeni komut eklemek icin:
#   1) Asagiya "def komut_xxx(text):" seklinde bir fonksiyon yaz
#      (is bitince True dondur).
#   2) KOMUT_ISLEYICILERI tablosuna  "KOD": komut_xxx  satirini ekle.
#   3) Dosyanin basindaki KOMUTLAR'a soyleyisleri  "...": "KOD"  olarak ekle.

GUNLER = ["Pazartesi", "Salı", "Çarşamba", "Perşembe",
          "Cuma", "Cumartesi", "Pazar"]
AYLAR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
         "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]


def komut_isik_ac(text):
    led.on()
    print(">>> LED ACILDI")
    konus("Işık açıldı.")
    return True


def komut_isik_kapat(text):
    led.off()
    print(">>> LED KAPANDI")
    konus("Işık kapatıldı.")
    return True


def komut_sicaklik(text):
    print("\n>>> BMP280 SICAKLIK OKUNUYOR")
    sicaklik = sicaklik_oku()

    if sicaklik is None:
        konus("Üzgünüm, sıcaklık sensöründen bilgi alamadım.")
    else:
        print(f">>> SICAKLIK: {sicaklik:.2f} C")
        konus(f"Şu anki sıcaklık {ondalikli_soyle(sicaklik)} derece.")
    return True


def komut_basinc(text):
    print("\n>>> BMP280 BASINC OKUNUYOR")
    basinc = basinc_oku()

    if basinc is None:
        konus("Üzgünüm, basınç bilgisini alamadım.")
    else:
        print(f">>> BASINC: {basinc:.1f} hPa")
        konus(f"Hava basıncı {round(basinc)} hektopaskal.")
    return True


def komut_saat(text):
    simdi = datetime.datetime.now()
    sa, dk = simdi.hour, simdi.minute

    if dk == 0:
        konus(f"Saat tam {sa}.")
    elif dk < 10:
        konus(f"Saat {sa} sıfır {dk}.")
    else:
        konus(f"Saat {sa} {dk}.")
    return True


def komut_tarih(text):
    bugun = datetime.date.today()
    konus(
        f"Bugün {bugun.day} {AYLAR[bugun.month - 1]} {bugun.year}, "
        f"{GUNLER[bugun.weekday()]}."
    )
    return True


def komut_sinif_listesi(text):
    print(">>> SINIF LİSTESİ")
    konus(", ".join(OGRENCILER) + ", sınıfa hoş geldiniz.")
    return True


_son_secilen = None


def komut_rastgele_ogrenci(text):
    global _son_secilen

    # Ayni ogrenci arka arkaya secilmesin
    adaylar = [o for o in OGRENCILER if o != _son_secilen] or OGRENCILER
    secilen = random.choice(adaylar)
    _son_secilen = secilen

    print(">>> SECILEN OGRENCI:", secilen)
    konus(f"Seçilen öğrenci: {secilen}.")
    return True


def komut_yardim(text):
    konus(
        "Şunları yapabilirim: ışığı açıp kapatabilirim, "
        "sıcaklığı ve hava basıncını söyleyebilirim, "
        "saati ve tarihi söyleyebilirim, sınıf listesini okuyabilir "
        "ve rastgele öğrenci seçebilirim, sayaç kurabilirim, "
        "istersen seni güldürmeye de çalışırım. "
        "Bir öğrencinin adını söylersen onu da kaydederim. "
        "Komut vermek için önce Merhaba Defne de."
    )
    return True


def komut_ogrenci(text):
    isim, _ = ogrenci_bul(text)
    if not isim:
        return False

    simdi = datetime.datetime.now()
    yol = os.path.join(BETIK_KLASORU, OGRENCI_KAYIT_DOSYASI)

    try:
        yeni = not os.path.exists(yol)
        # utf-8-sig: Excel Turkce karakterleri dogru gostersin
        with open(yol, "a", encoding="utf-8-sig" if yeni else "utf-8") as f:
            if yeni:
                f.write("tarih;saat;ogrenci\n")
            f.write(f"{simdi:%d.%m.%Y};{simdi:%H:%M:%S};{isim}\n")
        print(f">>> OGRENCI KAYDEDILDI: {isim}")
    except Exception as error:
        print("Kayit hatasi:", error)

    konus(OGRENCI_CEVABI.format(isim=isim))
    return True


_espri_destesi = []


def komut_espri(text):
    """Esprileri karistirip sirayla soyler; hepsi bitmeden tekrar etmez."""
    global _espri_destesi

    if not _espri_destesi:
        _espri_destesi = random.sample(SOGUK_ESPRILER, len(SOGUK_ESPRILER))
        # Yeni turun ilk esprisi bir oncekiyle ayni olmasin
        if len(_espri_destesi) > 1 and _espri_destesi[-1] == son_cevap:
            _espri_destesi.insert(0, _espri_destesi.pop())

    konus(_espri_destesi.pop())
    return True


def komut_tekrar(text):
    if son_cevap:
        konus(son_cevap)
    else:
        konus("Henüz bir şey söylemedim.", kaydet=False)
    return True


# ---------- Sayac ----------

SAYI_DEGERLERI = {
    "bir": 1, "iki": 2, "uc": 3, "dort": 4, "bes": 5, "alti": 6,
    "yedi": 7, "sekiz": 8, "dokuz": 9, "on": 10, "yirmi": 20,
    "otuz": 30, "kirk": 40, "elli": 50, "altmis": 60,
}
SURE_BIRIMLERI = {
    "dakika": 60, "dakikalik": 60,
    "saniye": 1, "saniyelik": 1,
}

_sayac = None            # threading.Timer
_sayac_bitis = 0.0
_sayac_aciklama = ""


def sure_coz(text):
    """'on bes dakika sayac kur' -> (900, '15 dakika'); bulunamazsa None."""
    words = text.split()

    for i, w in enumerate(words):
        if w not in SURE_BIRIMLERI:
            continue

        # Birimden onceki sayi kelimelerini topla ("on bes" -> 15)
        toplam = 0
        j = i - 1
        while j >= 0:
            if words[j].isdigit():
                toplam += int(words[j])
            elif words[j] in SAYI_DEGERLERI:
                toplam += SAYI_DEGERLERI[words[j]]
            else:
                break
            j -= 1

        if toplam > 0:
            birim = "dakika" if SURE_BIRIMLERI[w] == 60 else "saniye"
            return toplam * SURE_BIRIMLERI[w], f"{toplam} {birim}"

    return None


def sure_metni(saniye):
    saniye = int(round(saniye))
    dk, sn = divmod(saniye, 60)
    if dk and sn:
        return f"{dk} dakika {sn} saniye"
    if dk:
        return f"{dk} dakika"
    return f"{sn} saniye"


def _sayac_doldu():
    global _sayac
    _sayac = None
    print(f"\n>>> SAYAC BITTI ({_sayac_aciklama})")
    konus(f"Süre doldu! {_sayac_aciklama} tamamlandı.")


def komut_sayac(text):
    global _sayac, _sayac_bitis, _sayac_aciklama

    sonuc = sure_coz(text)
    if sonuc is None:
        tekrar_sor("Kaç dakikalık sayaç kurayım? Örneğin beş dakika sayaç kur de.")
        return False

    saniye, aciklama = sonuc

    # Onceki sayac varsa yenisiyle degistir
    if _sayac is not None:
        _sayac.cancel()

    _sayac = threading.Timer(saniye, _sayac_doldu)
    _sayac.daemon = True
    _sayac.start()
    _sayac_bitis = time.monotonic() + saniye
    _sayac_aciklama = aciklama

    print(f">>> SAYAC KURULDU: {aciklama}")
    konus(f"Tamam, {aciklama} sayaç kurdum.")
    return True


def komut_sayac_iptal(text):
    global _sayac

    if _sayac is None:
        konus("Çalışan bir sayaç yok.")
    else:
        _sayac.cancel()
        _sayac = None
        print(">>> SAYAC IPTAL")
        konus("Sayaç iptal edildi.")
    return True


def komut_sayac_kalan(text):
    if _sayac is None:
        konus("Çalışan bir sayaç yok.")
    else:
        kalan = max(0.0, _sayac_bitis - time.monotonic())
        konus(f"Sayacın bitmesine {sure_metni(kalan)} kaldı.")
    return True


def komut_dur(text):
    print(">>> DINLEME KAPANDI")
    konus("Dinleme sonlandırıldı.")
    return True


# Komut kodu -> calisacak fonksiyon
KOMUT_ISLEYICILERI = {
    "LIGHT_ON": komut_isik_ac,
    "LIGHT_OFF": komut_isik_kapat,
    "TEMPERATURE": komut_sicaklik,
    "PRESSURE": komut_basinc,
    "TIME": komut_saat,
    "DATE": komut_tarih,
    "CLASS": komut_sinif_listesi,
    "RANDOM_STUDENT": komut_rastgele_ogrenci,
    "HELP": komut_yardim,
    "REPEAT": komut_tekrar,
    "JOKE": komut_espri,
    "STUDENT": komut_ogrenci,
    "TIMER": komut_sayac,
    "TIMER_CANCEL": komut_sayac_iptal,
    "TIMER_LEFT": komut_sayac_kalan,
    "STOP": komut_dur,
}


def execute_command(text):
    global deneme_sayisi
    text = normalize(text)

    command, phrase, score = match_command(text)

    if command:
        print(
            f">>> Eslesme: '{text}' -> '{phrase}' "
            f"(skor {score:.2f})"
        )

    if command == "LIGHT_ASK":
        tekrar_sor("Işığı açayım mı, kapatayım mı?")
        return False

    isleyici = KOMUT_ISLEYICILERI.get(command)

    if isleyici is not None:
        sonuc = isleyici(text)
        if sonuc:
            deneme_sayisi = 0
        return sonuc

    print(
        f">>> UNKNOWN COMMAND: '{text}' "
        f"(en yakin: '{phrase}', skor {score:.2f})"
    )

    # Cok kisa / anlamsiz parcalarda (gurultu) konusma
    if len(text) >= 3:
        tekrar_sor("Anlayamadım, tekrar söyler misin?")

    return False

# ==========================================
# KONUSMA TANIMA
# ==========================================

_vosk_model = None
_vosk_gramer = None


def ogrenci_cumlelerini_hazirla():
    """Her ogrenci icin Vosk'un taniyabilecegi soyleyisi bulur.

    1) Sozlukte olmayan kelime, sozlukte olan iki kelimeye bolunmeye
       calisilir: "karakuzu" -> "kara kuzu", "bozdemir" -> "boz demir".
    2) Yine olmuyorsa kalan kelimelerle (genelde ad) taninir; ancak bu
       kisa ad sinifta tek ise ve cok kisa / gundelik bir kelime degilse.
    Taninamayan ogrenciler ekrana yazilir."""

    def var(w):
        return _vosk_model.vosk_model_find_word(w) >= 0

    def soyle(w):
        """Kelimeyi Vosk'un bildigi kelime(ler)le ifade eder; olmazsa None."""
        if var(w):
            return [w]
        en_iyi = None
        for k in range(2, len(w) - 1):
            a, b = w[:k], w[k:]
            if len(b) >= 2 and var(a) and var(b):
                # Parcalarin dengeli oldugu bolmeyi tercih et
                if en_iyi is None or min(len(a), len(b)) > min(map(len, en_iyi)):
                    en_iyi = [a, b]
        return en_iyi

    # Komutlarda gecen kelimeler tek basina ogrenci anahtari olmasin
    komut_kelimeleri = {w for c in COMMANDS for w in c.split()}
    komut_kelimeleri |= set(LIGHT_WORDS) | set(ON_WORDS) | set(OFF_WORDS)

    tam_anahtarlar = {normalize(o) for o in OGRENCILER}

    # Her ogrenci icin: (tam soyleyis veya None, kismi soyleyis)
    soyleyisler = {}
    for ogrenci in OGRENCILER:
        parcalar = [soyle(w) for w in tr_kucuk(ogrenci).split()]
        if all(parcalar):
            tam = " ".join(p for ps in parcalar for p in ps)
            soyleyisler[ogrenci] = (tam, None)
        else:
            kismi = " ".join(p for ps in parcalar if ps for p in ps)
            soyleyisler[ogrenci] = (None, kismi)

    kisa_sayac = {}
    for tam, kismi in soyleyisler.values():
        if kismi:
            k = normalize(kismi)
            kisa_sayac[k] = kisa_sayac.get(k, 0) + 1

    cumleler, bolunen, kisaltilan, taninamayan = [], [], [], []

    for ogrenci, (tam, kismi) in soyleyisler.items():
        if tam:
            cumleler.append(tam)
            anahtar = normalize(tam)
            if anahtar != normalize(ogrenci):
                OGRENCI_ANAHTARLARI[anahtar] = ogrenci
                bolunen.append(f"{ogrenci} -> '{tam}'")
            continue

        anahtar = normalize(kismi) if kismi else ""
        tek_kelime = len(anahtar.split()) == 1
        if (
            anahtar
            and kisa_sayac.get(anahtar) == 1
            and anahtar not in tam_anahtarlar
            and not (tek_kelime and (len(anahtar) < 4 or anahtar in komut_kelimeleri))
            # Ayni ad baska bir ogrencinin adinda da geciyorsa belirsiz
            and not any(
                f" {anahtar} " in f" {t} " for t in tam_anahtarlar
                if t != normalize(ogrenci)
            )
        ):
            cumleler.append(kismi)
            OGRENCI_ANAHTARLARI[anahtar] = ogrenci
            kisaltilan.append(f"{ogrenci} -> '{kismi}'")
        else:
            taninamayan.append(ogrenci)

    if bolunen:
        print("Bilgi: Su adlar bolunerek taninacak (soylerken fark yok):")
        for k in bolunen:
            print("   ", k)
    if kisaltilan:
        print("Bilgi: Su ogrenciler kisa adla taninacak (bu kismi soylemen yeterli):")
        for k in kisaltilan:
            print("   ", k)
    if taninamayan:
        print("UYARI: Su ogrenciler sesle taninamayacak:")
        print("    " + ", ".join(taninamayan))

    return cumleler


def vosk_hazirla():
    """Vosk modelini yukler ve komut listesinden gramer olusturur."""
    global _vosk_model, _vosk_gramer

    if _vosk_model is not None:
        return

    if VoskModel is None:
        print("HATA: vosk kurulu degil.  pip install vosk")
        sys.exit(1)

    if os.path.isdir(VOSK_MODEL_YOLU):
        print("Vosk modeli yukleniyor:", VOSK_MODEL_YOLU)
        _vosk_model = VoskModel(VOSK_MODEL_YOLU)
    else:
        print("Vosk Turkce modeli yukleniyor (ilk seferde indirilir)...")
        _vosk_model = VoskModel(lang="tr")

    if not GRAMER_MODU:
        print("Vosk hazir (serbest konusma modu).")
        return

    komutlar = list(KOMUTLAR) + SAYAC_CUMLELERI + ogrenci_cumlelerini_hazirla()
    cumleler = set(komutlar) | set(UYANDIRMA)
    for u in UYANDIRMA:
        for k in komutlar:
            cumleler.add(f"{u} {k}")

    # Modelin sozlugunde olmayan kelimeleri bul
    eksik = sorted({
        w for c in cumleler for w in c.split()
        if _vosk_model.vosk_model_find_word(w) < 0
    })
    if eksik:
        print("UYARI: Vosk sozlugunde olmayan kelimeler:", ", ".join(eksik))
        print("       Bu kelimeleri iceren cumleler gramerden cikarildi.")
        cumleler = {
            c for c in cumleler
            if not any(w in eksik for w in c.split())
        }

    _vosk_gramer = json.dumps(sorted(cumleler) + ["[unk]"], ensure_ascii=False)
    print(f"Vosk hazir (gramer modu, {len(cumleler)} cumle).")


def asr_metin(audio):
    start = time.monotonic()

    vosk_hazirla()

    if _vosk_gramer:
        rec = KaldiRecognizer(_vosk_model, ASR_RATE, _vosk_gramer)
    else:
        rec = KaldiRecognizer(_vosk_model, ASR_RATE)

    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
    rec.AcceptWaveform(pcm)
    text = json.loads(rec.FinalResult()).get("text", "")

    # Gramer modunda taninamayan kisimlar "[unk]" olarak gelir
    text = text.replace("[unk]", " ")

    print("\nDuyulan:", text.strip() or "(anlasilmadi)")
    print(f"Tanima suresi: {time.monotonic() - start:.2f}s")

    return normalize(text)


def metni_isle(text):
    global state, wake_deadline, deneme_sayisi

    now = time.monotonic()

    # Uyandirma suresi doldu mu?
    if (
        state == "COMMAND"
        and now > wake_deadline
    ):
        state = "SLEEP"
        print(
            "Wake timeout. "
            "Merhaba Defne bekleniyor..."
        )

    if not text:
        return

    remaining = find_wake(text)

    # --------------------------
    # UYKU MODU
    # --------------------------

    if state == "SLEEP":

        if remaining is None:
            return

        print(
            "\n>>> MERHABA DEFNE ALGILANDI"
        )

        deneme_sayisi = 0

        # Tek cumlede uyandirma + komut
        if remaining:
            success = execute_command(
                remaining
            )

            if success:
                state = "SLEEP"
                print(
                    "Merhaba Defne bekleniyor..."
                )
            else:
                state = "COMMAND"
                wake_deadline = (
                    time.monotonic()
                    + WAKE_TIMEOUT
                )
                print("Listening for command...")

        # Yalnizca Merhaba Defne
        else:
            # Yaniti bitirene kadar
            # komut modu sure saymasin
            # Kullanici "Merhaba Defne"den sonra komutu soylemeye devam
            # ediyorsa "Dinliyorum" deyip mikrofonu kapatma; yoksa
            # komut kaybolur. Siradaki parca komut olarak islenir.
            if globals().get("recording") or not speech_queue.empty():
                print("Komut devam ediyor, dinlemeye devam...")
            else:
                konus("Dinliyorum.")

            state = "COMMAND"

            wake_deadline = (
                time.monotonic()
                + WAKE_TIMEOUT
            )

            print("Listening for command...")

    # --------------------------
    # KOMUT MODU
    # --------------------------

    elif state == "COMMAND":

        if remaining is not None:
            text = remaining

        if not text:
            print("Listening for command...")
            return

        success = execute_command(text)

        if success:
            state = "SLEEP"

            print(
                "Merhaba Defne bekleniyor..."
            )

        else:
            print(
                "Command not recognized. "
                "Try again..."
            )



# ==========================================
# WHISTLE WORKER
# ==========================================

def speech_worker():
    global state, wake_deadline, deneme_sayisi

    while not stop_event.is_set():

        try:
            audio = speech_queue.get(
                timeout=0.2
            )
        except queue.Empty:
            continue

        try:
            if mikrofon_sessiz():
                continue

            text = asr_metin(audio)
            metni_isle(text)

        except Exception as error:
            print("\nWhistle error:", error)

        finally:
            speech_queue.task_done()

# ==========================================
# MIKROFON CALLBACK
# ==========================================

def callback(indata, frames, time_info, status):
    global audio_overflows

    if status:
        audio_overflows += 1

    if mikrofon_sessiz():
        return

    try:
        audio_queue.put_nowait(
            bytes(indata)
        )

    except queue.Full:
        audio_overflows += 1
# ==========================================
# BASLATMA
# ==========================================

worker = threading.Thread(
    target=speech_worker,
    daemon=True
)

worker.start()

blocksize = int(
    MIC_RATE * BLOCK_MS / 1000
)

preroll = deque(maxlen=3)

recording = False
speech_buffer = bytearray()

silence = 0.0
active_blocks = 0

print("\n================================")
print("MERHABA DEFNE - TURKCE SESLI ASISTAN")
print("================================")

print("Microphone:", MIC_RATE, "Hz")
print("ASR: Vosk", "(gramer modu)" if GRAMER_MODU else "(serbest)")
print("TTS:", "EMA Lightning" if tts else "Windows sistem sesi")
print("Platform:", platform.system())
print("GPIO:", LED_PIN)
print("Dil: Turkce (Vosk)")
print("Merhaba Defne bekleniyor...")
print("Press CTRL+C to exit")

# ==========================================
# METIN MODU (mikrofonsuz test)
# ==========================================

if METIN_MODU:
    print("\nMETIN MODU: komutu yazip Enter'a bas.")
    print("Ornek: merhaba defne ışığı aç   (cikmak icin: q)")
    try:
        while True:
            satir = input("\nSen> ").strip()
            if satir.lower() in ("q", "quit", "exit"):
                break
            metni_isle(normalize(satir))
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        led.off()
        led.close()
    sys.exit(0)

# ==========================================
# ANA DINLEME DONGUSU
# ==========================================

vosk_hazirla()

try:
    with sd.RawInputStream(
        device=MIC_DEVICE,
        samplerate=MIC_RATE,
        channels=1,
        dtype="int16",
        blocksize=blocksize,
        latency="high",
        callback=callback
    ):

        while True:

            try:
                data = audio_queue.get(
                    timeout=0.2
                )
            except queue.Empty:
                data = None

            if mikrofon_sessiz():
                recording = False
                speech_buffer = bytearray()
                preroll.clear()
                silence = 0.0
                active_blocks = 0
                continue

            if data is None:
                continue

            samples = np.frombuffer(
                data,
                dtype=np.int16
            ).astype(np.float32)

            rms = float(
                np.sqrt(np.mean(samples ** 2))
            )

            active = (
                rms > VOLUME_THRESHOLD
            )

            duration = (
                len(samples) / MIC_RATE
            )

            # --------------------------
            # KONUSMA BASLANGICI
            # --------------------------

            if not recording:

                preroll.append(data)

                if active:
                    active_blocks += 1
                else:
                    active_blocks = 0

                if active_blocks >= 2:
                    recording = True

                    speech_buffer = bytearray(
                        b"".join(preroll)
                    )

                    silence = 0.0
                    active_blocks = 0

                    print("\nSpeech detected...")

            # --------------------------
            # KONUSMA DEVAM EDIYOR
            # --------------------------

            else:
                speech_buffer.extend(data)

                if active:
                    silence = 0.0
                else:
                    silence += duration

                speech_duration = (
                    len(speech_buffer)
                    / (MIC_RATE * 2)
                )

                finished = (
                    silence >= SILENCE_SECONDS
                    or speech_duration >= MAX_SPEECH_SECONDS
                )

                if finished:

                    recording = False
                    preroll.clear()

                    if (
                        speech_duration
                        >= MIN_SPEECH_SECONDS
                    ):

                        raw_samples = np.frombuffer(
                            speech_buffer,
                            dtype=np.int16
                        ).astype(np.float32)

                        # 44100 Hz -> 16000 Hz
                        converted = resample_poly(
                            raw_samples,
                            160,
                            441
                        )

                        audio16 = np.clip(
                            converted / 32768.0,
                            -1.0,
                            1.0
                        ).astype(np.float32)

                        try:
                            speech_queue.put_nowait(
                                audio16
                            )

                            print("Sent to Whistle.")

                        except queue.Full:
                            print(
                                "Whistle busy: "
                                "speech dropped."
                            )

                    speech_buffer = bytearray()
                    silence = 0.0

except KeyboardInterrupt:
    print("\nAssistant stopped.")

finally:
    stop_event.set()

    led.off()
    led.close()

    print(
        "Audio overflow count:",
        audio_overflows
    )
