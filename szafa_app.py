import streamlit as st
import firebase_admin
from firebase_admin import credentials, firestore, storage
import vertexai
from vertexai.generative_models import GenerativeModel, Part, SafetySetting, HarmCategory, HarmBlockThreshold
from google.oauth2 import service_account
import json
import os
import uuid
import time
import io
import datetime
import requests
from PIL import Image, ImageDraw

# --- 1. KONFIGURACJA STRONY I FOLDERÓW ---
st.set_page_config(page_title="Wirtualna Szafa Magdy", layout="wide", page_icon="👗")

IMAGE_DIR = "wardrobe_images"
os.makedirs(IMAGE_DIR, exist_ok=True)
# Lokalna ścieżka dla zdjęcia bazowego Magdy (wgrywane jednorazowo w sesji)
BASE_IMG_PATH = os.path.join(IMAGE_DIR, "magda_base.jpg") 

# --- 2. INICJALIZACJA BAZY, STORAGE I VERTEX AI ---
@st.cache_resource
def init_services():
    creds_dict = json.loads(st.secrets["FIREBASE_CREDS"])
    creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
    
    # Inicjalizacja z Twoim nowym zasobnikiem Google Cloud Storage
    if not firebase_admin._apps:
        cred = credentials.Certificate(creds_dict)
        firebase_admin.initialize_app(cred, {
            'storageBucket': "szafa-magdy-zdjecia-2026"
        })
    db = firestore.client()
    bucket = storage.bucket() 

    vertex_creds = service_account.Credentials.from_service_account_info(creds_dict)
    vertexai.init(
        project=st.secrets.get("GCP_PROJECT_ID", "roboczy-bez-limitu"),
        location="us-central1",
        credentials=vertex_creds
    )
    return db, bucket

db, bucket = init_services()

# --- 3. BRAMKA HASŁA ---
if "password_correct" not in st.session_state:
    st.session_state.password_correct = False

if not st.session_state.password_correct:
    st.header("👗 Wirtualna Szafa — Logowanie")
    pwd = st.text_input("Hasło dostępu:", type="password")
    if st.button("Zaloguj"):
        # Używamy hasła z Twoich sekretów
        if pwd == st.secrets.get("ADMIN_PASSWORD", "magda277"):
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
    col_header, col_nuke = st.columns([3, 1])
    with col_header:
        st.subheader("Wszystkie ubrania w bazie")
    
    # OPCJE ZAAWANSOWANE (USUWANIE WSZYSTKIEGO)
    with st.expander("⚠️ Opcje zaawansowane"):
        st.warning("Ta akcja nieodwracalnie usunie WSZYSTKIE ubrania z bazy danych i chmury!")
        if st.button("🚨 Wyczyść całą szafę", type="primary"):
            with st.spinner("Usuwanie danych..."):
                all_items = db.collection("wardrobe_items").stream()
                for doc in all_items:
                    item_data = doc.to_dict()
                    image_url = item_data.get("image_path", "")
                    
                    if image_url.startswith("http"):
                        try:
                            # Wyciągnięcie nazwy pliku z podpisanego URL
                            blob_name = image_url.split("?")[0].split("/")[-1]
                            blob = bucket.blob(f"wardrobe_images/{blob_name}")
                            blob.delete()
                        except:
                            pass
                    db.collection("wardrobe_items").document(doc.id).delete()
                
                st.success("Szafa wyczyszczona!")
                time.sleep(1)
                st.rerun()
    
    st.markdown("---")

    items_ref = db.collection("wardrobe_items").stream()
    items = [{"id": doc.id, **doc.to_dict()} for doc in items_ref]
    
    if not items:
        st.info("Szafa jest pusta.")
    else:
        cols = st.columns(4)
        for i, item in enumerate(items):
            with cols[i % 4]:
                st.markdown(f"**ID: {item['id']}** | {item.get('kategoria', 'Ubranie')}")
                
                image_url = item.get("image_path", "")
                if image_url.startswith("http"):
                    st.image(image_url, use_container_width=True)
                else:
                    st.error("⚠️ Brak obrazu w chmurze.")
                
                st.caption(f"**Typ:** {item.get('typ_szczegolowy', '')}")
                st.caption(f"**Wygląd:** {item.get('kolor_wzor', '')}")
                
                with st.expander("🛠️ Detale AI"):
                    st.write(f"**Materiał:** {item.get('material_faktura', '')}")
                    st.write(f"**Detale:** {item.get('detale', '')}")
                    st.code(item.get('opis_dla_vto', ''))
                
                if st.button("🗑️ Usuń", key=f"del_{item['id']}"):
                    db.collection("wardrobe_items").document(item['id']).delete()
                    if image_url.startswith("http"):
                        try:
                            blob_name = image_url.split("?")[0].split("/")[-1]
                            blob = bucket.blob(f"wardrobe_images/{blob_name}")
                            blob.delete()
                        except:
                            pass
                    st.rerun()
                st.divider()

# ==========================================
# ZAKŁADKA 2: DODAJ (ZAPIS W CHMURZE)
# ==========================================
with tab_dodaj:
    st.subheader("Dodaj nowe ubrania")
    uploaded_files = st.file_uploader("Wgraj zdjęcia:", type=["jpg", "png", "jpeg"], accept_multiple_files=True)
    
    if uploaded_files:
        if st.button("🤖 Rozpoznaj i zapisz", type="primary"):
            for uploaded_file in uploaded_files:
                with st.spinner(f"Analiza: {uploaded_file.name}..."):
                    try:
                        file_id = str(uuid.uuid4())[:8]
                        file_ext = uploaded_file.name.split('.')[-1]
                        
                        # Upload do Google Cloud Storage
                        blob = bucket.blob(f"wardrobe_images/{file_id}.{file_ext}")
                        blob.upload_from_string(uploaded_file.getvalue(), content_type=uploaded_file.type)
                        # Link ważny 100 lat
                        image_url = blob.generate_signed_url(expiration=datetime.timedelta(days=36500))
                            
                        # Analiza Gemini
                        model = GenerativeModel("gemini-2.5-flash")
                        image_part = Part.from_data(uploaded_file.getvalue(), mime_type=uploaded_file.type)
                        
                        prompt = """
                        Zwróć WYŁĄCZNIE czysty JSON:
                        {
                            "kategoria": "Góra/Dół/Buty/Sukienka/Okrycie",
                            "typ_szczegolowy": "np. koszula jeansowa",
                            "kolor_wzor": "np. jasnoniebieski jeans",
                            "material_faktura": "np. miękki denim",
                            "detale": "np. perłowe napy",
                            "styl_sezon": "Casual",
                            "opis_dla_vto": "Detailed English description for image generation (25 words)."
                        }
                        """
                        response = model.generate_content([image_part, prompt])
                        tags = json.loads(response.text.replace("```json", "").replace("```", "").strip())
                        
                        db.collection("wardrobe_items").document(file_id).set({
                            **tags,
                            "image_path": image_url 
                        })
                        st.toast(f"✅ Dodano: {tags.get('typ_szczegolowy')}")
                        
                    except Exception as e:
                        st.error(f"Błąd: {e}")
            st.rerun()

# ==========================================
# ZAKŁADKA 3: DOBIERZ I WIZUALIZUJ
# ==========================================
with tab_dobierz:
    st.subheader("Wirtualny Stylista")
    
    st.markdown("#### 1. Zdjęcie bazowe Magdy")
    if os.path.exists(BASE_IMG_PATH):
        col_b1, col_b2 = st.columns([1, 4])
        with col_b1:
            st.image(BASE_IMG_PATH, width=150)
        with col_b2:
            if st.button("🗑️ Zmień zdjęcie"):
                os.remove(BASE_IMG_PATH)
                st.rerun()
    else:
        base_uploader = st.file_uploader("Wgraj swoje zdjęcie (sylwetka):", type=["jpg", "png", "jpeg"])
        if base_uploader:
            with open(BASE_IMG_PATH, "wb") as f:
                f.write(base_uploader.getbuffer())
            st.rerun()
    
    if os.path.exists(BASE_IMG_PATH):
        st.markdown("#### 2. Dobór ubrań")
        okazja = st.text_input("Na jaką okazję szukamy stroju?")
        
        if okazja and st.button("✨ Dobierz zestaw", type="primary"):
            items_ref = db.collection("wardrobe_items").stream()
            wardrobe_data = [{"id": doc.id, **doc.to_dict()} for doc in items_ref]
            
            if wardrobe_data:
                model_text = GenerativeModel("gemini-2.5-pro")
                prompt_wybor = f"Szafa: {json.dumps(wardrobe_data)}. Okazja: {okazja}. Zwróć JSON: ['id1', 'id2']"
                resp = model_text.generate_content(prompt_wybor)
                try:
                    selected_ids = json.loads(resp.text.replace("```json", "").replace("```", "").strip())
                    st.session_state.selected_items = [i for i in wardrobe_data if i['id'] in selected_ids]
                except:
                    st.error("AI miało problem z wyborem.")

        if "selected_items" in st.session_state and st.session_state.selected_items:
            st.markdown("#### Wybrany zestaw:")
            cols = st.columns(len(st.session_state.selected_items))
            vto_desc = ""
            valid_images = [Image.open(BASE_IMG_PATH).convert("RGB")]

            for i, item in enumerate(st.session_state.selected_items):
                with cols[i]:
                    url = item.get("image_path", "")
                    st.image(url, width=150)
                    resp = requests.get(url)
                    valid_images.append(Image.open(io.BytesIO(resp.content)).convert("RGB"))
                    vto_desc += f"- {item.get('opis_dla_vto', '')}\n"
            
            st.markdown("---")
            # GENEROWANIE KOLARZU
            target_h = 400
            resized = []
            for img in valid_images:
                new_w = int(target_h * (img.width / img.height))
                resized.append(img.resize((new_w, target_h)))
            
            total_w = sum(i.width for i in resized) + (20 * (len(resized)-1))
            collage = Image.new('RGB', (total_width, target_h), (255, 255, 255))
            curr_x = 0
            draw = ImageDraw.Draw(collage)
            for idx, img in enumerate(resized):
                collage.paste(img, (curr_x, 0))
                curr_x += img.width + 20
                if idx == 0: # Linia po zdjęciu Magdy
                    draw.line([(curr_x-10, 0), (curr_x-10, target_h)], fill="black", width=6)

            st.image(collage, caption="Twój zestaw do przymiarki", use_container_width=True)
            
            buf = io.BytesIO()
            collage.save(buf, format="JPEG")
            st.download_button("📥 Pobierz Kolarz", buf.getvalue(), "zestaw.jpg", "image/jpeg")

            prompt_final = f"Użyj modelu Nano Banana 2. Na zdjęciu po lewej jestem ja, po prawej ubrania. Ubierz mnie w nie:\n{vto_desc}"
            st.code(prompt_final)
