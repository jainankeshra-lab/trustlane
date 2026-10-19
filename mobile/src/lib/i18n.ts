// English and Hindi. English text is the key, so a missing translation shows English, never a blank.
// Placeholders: {name} -> vars.name.
import { getLocales } from 'expo-localization';

export type Lang = 'en' | 'hi';

const HI: Record<string, string> = {
  // start, consent
  'Scan as you shop. Walk straight out.': 'सामान स्कैन करें, सीधे बाहर निकलें।',
  "You're charged automatically when you leave.": 'बाहर निकलते ही भुगतान अपने आप हो जाता है।',
  'At the exit gate you approve one UPI payment.': 'निकास गेट पर आप एक UPI भुगतान स्वीकृत करते हैं।',
  'Before you start: how this store checks baskets': 'शुरू करने से पहले: यह स्टोर टोकरी की जाँच कैसे करता है',
  'Shelf cameras look at the shelf front and hands only. They never record faces, and video stays on the shelf device.':
    'शेल्फ कैमरे सिर्फ़ शेल्फ के सामने और हाथ देखते हैं। चेहरे कभी रिकॉर्ड नहीं होते, और वीडियो शेल्फ डिवाइस पर ही रहता है।',
  "Shelves record which product was taken. Your phone shares which aisle you're in (from aisle beacons) and broadcasts a rotating Bluetooth ID that only the store can link to this trip.":
    'शेल्फ दर्ज करती है कि कौन-सा सामान उठाया गया। आपका फ़ोन बताता है कि आप किस गलियारे में हैं, और एक बदलती ब्लूटूथ ID भेजता है जिसे सिर्फ़ स्टोर इस यात्रा से जोड़ सकता है।',
  'Checks use what happened on this trip only, never who you are or how you look. You can dispute any check from the app.':
    'जाँच केवल इस यात्रा की घटनाओं पर आधारित होती है, आप कौन हैं या कैसे दिखते हैं इस पर नहीं। आप ऐप से किसी भी जाँच पर आपत्ति कर सकते हैं।',
  "Trip records are kept for 90 days for audit, under India's DPDP Act 2023.":
    'भारत के DPDP अधिनियम 2023 के तहत यात्रा के रिकॉर्ड ऑडिट के लिए 90 दिन रखे जाते हैं।',
  'I understand and agree': 'मैं समझता/समझती हूँ और सहमत हूँ',
  'Start trip': 'यात्रा शुरू करें',
  'Settings': 'सेटिंग्स',
  'Account': 'खाता',
  // shopping
  'Your cart': 'आपकी कार्ट',
  'Live': 'लाइव',
  'Reconnecting': 'फिर से जुड़ रहा है',
  'Where are you?': 'आप कहाँ हैं?',
  'Choose your aisle': 'अपना गलियारा चुनें',
  'Beacon: aisle {aisle}': 'बीकन: गलियारा {aisle}',
  'Aisle {aisle}': 'गलियारा {aisle}',
  'Entry': 'प्रवेश',
  'Scan a barcode': 'बारकोड स्कैन करें',
  'Cart': 'कार्ट',
  'Nothing scanned yet.': 'अभी तक कुछ स्कैन नहीं हुआ।',
  'Go to exit': 'निकास की ओर जाएँ',
  'Put back': 'वापस रखें',
  'Put it on the shelf': 'इसे शेल्फ पर रखें',
  'Oops!': 'ओह!',
  'Scan it now': 'अभी स्कैन करें',
  "I didn't take it": 'मैंने नहीं लिया',
  'Dismiss': 'हटाएँ',
  'Verified ✓': 'सत्यापित ✓',
  'Scanned': 'स्कैन हुआ',
  'Weighing…': 'तौल रहे हैं…',
  'Check': 'जाँचें',
  'Removed': 'हटाया गया',
  'Refunded': 'वापस किया गया',
  'Offers today': 'आज के ऑफ़र',
  'Coupon code': 'कूपन कोड',
  'Apply': 'लागू करें',
  'Applied ✓': 'लागू ✓',
  'You have {points} points': 'आपके पास {points} पॉइंट हैं',
  'Use points': 'पॉइंट इस्तेमाल करें',
  "Don't use": 'इस्तेमाल न करें',
  'Includes GST {amount}': 'GST {amount} शामिल',
  'You saved {amount}': 'आपने {amount} बचाए',
  'An item in your cart is age-restricted. Please show your ID to a team member before you leave.':
    'आपकी कार्ट में आयु-प्रतिबंधित सामान है। निकलने से पहले कृपया स्टाफ़ को अपनी ID दिखाएँ।',
  "{n} scan(s) saved on your phone while offline. They'll sync automatically.":
    'ऑफ़लाइन रहते {n} स्कैन फ़ोन पर सहेजे गए। ये अपने आप सिंक हो जाएँगे।',
  // exit and after
  'Exit': 'निकास',
  'Show this code at the gate scanner': 'गेट स्कैनर पर यह कोड दिखाएँ',
  'Total': 'कुल',
  '{n} item(s)': '{n} सामान',
  'Tap your phone on the gate (NFC)': 'फ़ोन को गेट पर छुआएँ (NFC)',
  'Back to shopping': 'खरीदारी पर वापस',
  'Quick check': 'छोटी जाँच',
  'Did you take these?': 'क्या आपने ये लिए?',
  "The shelf recorded these items but we didn't see a scan. Add anything you have.":
    'शेल्फ ने ये सामान दर्ज किए पर स्कैन नहीं दिखा। जो आपके पास है उसे जोड़ें।',
  'I have it, add it': 'मेरे पास है, जोड़ें',
  'Confirm and pay': 'पुष्टि करें और भुगतान करें',
  'Almost done': 'बस हो गया',
  'A team member will be with you in a moment.': 'स्टाफ़ का एक सदस्य जल्द ही आपके पास आएगा।',
  'Paid · gate open': 'भुगतान हो गया · गेट खुला',
  'Thanks for shopping!': 'खरीदारी के लिए धन्यवाद!',
  'Total charged': 'कुल शुल्क',
  'View GST invoice {number}': 'GST बिल देखें {number}',
  '+{earned} points · balance {balance}': '+{earned} पॉइंट · शेष {balance}',
  'Start a new trip': 'नई यात्रा शुरू करें',
  'Something not right?': 'कुछ गलत लगा?',
  'Tell us what happened': 'बताइए क्या हुआ',
  'Send to the store team': 'स्टोर टीम को भेजें',
  "Your payment didn't go through": 'आपका भुगतान नहीं हो पाया',
  'Try again': 'फिर से कोशिश करें',
  // account
  'Your account': 'आपका खाता',
  'Sign in with your mobile number to keep your receipts and points on any phone.':
    'अपनी रसीदें और पॉइंट किसी भी फ़ोन पर रखने के लिए मोबाइल नंबर से साइन इन करें।',
  'Mobile number': 'मोबाइल नंबर',
  'Send code': 'कोड भेजें',
  '6-digit code': '6 अंकों का कोड',
  'Sign in': 'साइन इन करें',
  'E-mail for invoices': 'बिल के लिए ई-मेल',
  'Save': 'सहेजें',
  'Loyalty points': 'लॉयल्टी पॉइंट',
  'Receipts': 'रसीदें',
  'No receipts yet.': 'अभी कोई रसीद नहीं।',
  'Your data (DPDP Act 2023)': 'आपका डेटा (DPDP अधिनियम 2023)',
  'Download my data': 'मेरा डेटा डाउनलोड करें',
  'Delete my account and data': 'मेरा खाता और डेटा हटाएँ',
  'Signed in · {phone}': 'साइन इन · {phone}',
  // settings
  'Language': 'भाषा',
  'Send me a test notification': 'मुझे एक टेस्ट सूचना भेजें',
  'Store server': 'स्टोर सर्वर',
  'Save and connect': 'सहेजें और जोड़ें',
  'Status': 'स्थिति',
};

const TABLES: Record<Lang, Record<string, string>> = { en: {}, hi: HI };

export function deviceLang(): Lang {
  try {
    return getLocales()[0]?.languageCode === 'hi' ? 'hi' : 'en';
  } catch {
    return 'en';
  }
}

export function translate(lang: Lang, text: string, vars?: Record<string, string | number>): string {
  let out = TABLES[lang][text] ?? text;
  for (const [k, v] of Object.entries(vars ?? {})) out = out.split(`{${k}}`).join(String(v));
  return out;
}

export function missing(lang: Lang, keys: string[]): string[] {
  return keys.filter((k) => !(k in TABLES[lang]));
}
