import streamlit as st
import firebase_admin
from firebase_admin import credentials, firestore
import vertexai
from vertexai.generative_models import GenerativeModel, Part, SafetySetting, HarmCategory, HarmBlockThreshold
from google.oauth2 import service_account
import json
import os
import uuid
import time
import io
from PIL import Image, ImageDraw

# --- 1. KONFIGURACJA STRONY I FOLDERÓW ---
st.set_page_config(page_title="Wirtualna Szafa Magdy", layout="wide", page_icon="👗")

IMAGE_DIR = "wardrobe_images"
os.makedirs(IMAGE_DIR, exist_ok=True)

# --- 2. INICJALIZACJA BAZY I VERTEX AI ---
@st.cache_resource
def init_services():
    creds_dict = json.loads(st.secrets["FIREBASE_CREDS"])
    
    # Naprawiamy znaki nowej linii w kluczu prywatnym
    creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
    
    # Firebase
    if not firebase_admin._apps:
        cred = credentials.Certificate(creds_dict)
        firebase_admin.initialize_app(cred)
    db = firestore.client()

    # Vertex AI (Zostawiamy do generowania tagów tekstowych)
    vertex_creds = service_account.Credentials.from_service_account_info(creds_dict)
    vertexai.init(
        project=st.secrets.get("GCP_PROJECT_ID", "roboczy-bez-limitu"),
        location="us-central1",
        credentials=vertex_creds
    )
    return db

db = init_services()

# --- 3. BRAMKA HASŁA ---
if "password_correct" not in st.session_state:
    st.session_state.password_correct = False

if not st.session_state.password_correct:
    st.header("👗 Wirtualna Szafa — Logowanie")
    pwd = st.text_input("Hasło dostępu:", type="password")
    if st.button("Zaloguj"):
        if pwd == st.secrets.get("ADMIN_PASSWORD", "DUNAJEC30"):
            st.session_state.password_correct = True
            st.rerun()
        else:
            st.error("Błędne hasło")
    st.stop()

# --- 4. GŁÓWNY INTERFEJS ---
st.title("👗 Wirtualna Szafa Magdy")

tab_przeglad, tab_dodaj, tab_dobierz = st.tabs([
    "👚 Przegląd Szafy", 
    "📸 Dodaj Nowe", 
    "✨ Dobierz Strój i Wizualizuj"
])

# ==========================================
# ZAKŁADKA 1: PRZEGLĄD
# ==========================================
with tab_przeglad:
    st.subheader("Wszystkie ubrania w bazie")
    
    items_ref = db.collection("wardrobe_items").stream()
    items = [{"id": doc.id, **doc.to_dict()} for doc in items_ref]
    
    if not items:
        st.info("Szafa jest pusta. Dodaj ubrania w zakładce 'Dodaj Nowe'.")
    else:
        cols = st.columns(4)
        for i, item in enumerate(items):
            with cols[i % 4]:
                st.markdown(f"**ID: {item['id']}** | {item.get('kategoria', 'Ubranie')}")
                if os.path.exists(item.get("image_path", "")):
                    st.image(item["
