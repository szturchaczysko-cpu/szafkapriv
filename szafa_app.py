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
BASE_IMG_PATH = os.path.join(IMAGE_DIR, "magda_base.jpg") # Lokalne zdjęcie bazowe zostaje, bo wgrywasz je raz przed sesją

# --- 2. INICJALIZACJA BAZY, STORAGE I VERTEX AI ---
@st.cache_resource
def init_services():
    creds_dict = json.loads(st.secrets["FIREBASE_CREDS"])
    creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
    
    if not firebase_admin._apps:
        cred = credentials.Certificate(creds_dict)
        firebase_admin.initialize_app(cred, {
            'storageBucket': f"{st.secrets.get('GCP_PROJECT_ID')}.appspot.com"
        })
    db = firestore.client()
    bucket = storage.bucket() 

    vertex_creds = service_account.Credentials.from_service_account_info(creds_dict)
    vertexai.init(
        project=st.secrets.get("GCP_PROJECT_ID"),
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
    col_header, col_nuke = st.columns([3, 1])
    with col_header:
        st.subheader("Wszystkie ubrania w bazie")
    
    # --- PRZYCISK NUKLEARNY (USUŃ WSZYSTKO) ---
    with st.expander("⚠️ Opcje zaawansowane"):
        st.warning("Ta akcja nieodwracalnie usunie WSZYSTKIE ubrania z bazy danych i chmury!")
        if st.button("🚨 Wyczyść całą szafę", type="primary"):
            with st.spinner("Usuwanie wszystkich ubrań i zdjęć... To może chwilę potrwać."):
                all_items = db.collection("wardrobe_items").stream()
                for doc in all_items:
                    item_data = doc.to_dict()
                    image_url = item_data.get("image_path", "")
                    
                    # 1. Usunięcie zdjęcia z chmury Firebase
                    if image_url.startswith("http"):
                        try:
                            blob_name = image_url.split("?")[0].split("/")[-1]
                            blob = bucket.blob(f"wardrobe_images/{blob_name}")
                            blob.delete()
                        except:
                            pass
                    
                    # 2. Usunięcie wpisu z Firestore
                    db.collection("wardrobe_items").document(doc.id).delete()
                
                st.success("Szafa została całkowicie wyczyszczona!")
                time.sleep(1.5)
                st.rerun()
    
    st.markdown("---")

    items_ref = db.collection("wardrobe_items").stream()
    items = [{"id": doc.id, **doc.to_dict()} for doc in items_ref]
    
    if not items:
        st.info("Szafa jest pusta. Dodaj ubrania w zakładce 'Dodaj Nowe'.")
    else:
        cols = st.columns(4)
        for i, item in enumerate(items):
            with cols[i % 4]:
                st.markdown(f"**ID: {item['id']}** | {item.get('kategoria', 'Ubranie')}")
                
                image_url = item.get("image_path", "")
                if image_url.startswith("http"):
                    st.image(image_url, use_container_width=True)
                elif os.path.exists(image_url):
                    st.image(image_url, use_container_width=True)
                else:
                    st.error("⚠️ Brak obrazu w chmurze.")
                
                st.caption(f"**Typ:** {item.get('typ_szczegolowy', 'Nieznany')}")
                st.caption(f"**Wygląd:** {item.get('kolor_wzor', '')}, {item.get('material_faktura', '')}")
                st.caption(f"**Detale:** {item.get('detale', '')}")
                
                with st.expander("🛠️ Zobacz ukryty prompt"):
                    st.code(item.get('opis_dla_vto', 'Brak szczegółowego opisu.'))
                
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
# ZAKŁADKA 2: DODAJ (ZAPIS W CHMURZE FIREBASE)
# ==========================================
with tab_dodaj:
    st.subheader("Dodaj nowe ubrania do kartoteki")
    uploaded_files = st.file_uploader("Wybierz zdjęcia ubrań (możesz zaznaczyć kilka)", type=["jpg", "png", "jpeg"], accept_multiple_files=True)
    
    if uploaded_files:
        if st.button("🤖 Rozpoznaj i zapisz wszystkie do bazy", type="primary"):
            for uploaded_file in uploaded_files:
                with st.spinner(f"Ekspert AI analizuje zdjęcie: {uploaded_file.name}..."):
                    try:
                        file_id = str(uuid.uuid4())[:8]
                        file_ext = uploaded_file.name.split('.')[-1]
                        
                        blob = bucket.blob(f"wardrobe_images/{file_id}.{file_ext}")
                        blob.upload_from_string(uploaded_file.getvalue(), content_type=uploaded_file.type)
                        image_url = blob.generate_signed_url(expiration=datetime.timedelta(days=36500))
                            
                        model = GenerativeModel("gemini-2.5-flash")
                        image_part = Part.from_data(uploaded_file.getvalue(), mime_type=uploaded_file.type)
                        
                        prompt = """
                        Jesteś ekspertem modowym. Zbadaj dokładnie to ubranie/buty z dbałością o najwyższe detale.
                        Zwróć WYŁĄCZNIE czysty JSON bez znaczników markdown, o następującej strukturze:
                        {
                            "kategoria": "np. Góra, Dół, Okrycie wierzchnie, Buty, Dodatek",
                            "typ_szczegolowy": "np. botki na grubym obcasie, sweter oversize, plisowana spódnica",
                            "kolor_wzor": "np. gładki czarny, biały w granatowe paski, butkowa zieleń",
                            "material_faktura": "np. gładka skóra licowa, gruby splot wełniany, zamsz",
                            "detale": "np. złote klamry, wiązanie na kostce, głęboki dekolt V",
                            "styl_sezon": "np. Smart casual / Jesień, Elegancki / Całoroczne",
                            "opis_dla_vto": "Kluczowe: Szczegółowy opis w języku angielskim (20-30 słów), służący jako prompt dla generatora obrazów (np. 'A pair of black smooth leather ankle boots with a 5cm block heel, featuring a silver side zipper and a rounded toe')."
                        }
                        """
                        response = model.generate_content([image_part, prompt])
                        
                        json_str = response.text.replace("```json", "").replace("```", "").strip()
                        tags = json.loads(json_str)
                        
                        db.collection("wardrobe_items").document(file_id).set({
                            "kategoria": tags.get("kategoria"),
                            "typ_szczegolowy": tags.get("typ_szczegolowy"),
                            "kolor_wzor": tags.get("kolor_wzor"),
                            "material_faktura": tags.get("material_faktura"),
                            "detale": tags.get("detale"),
                            "styl_sezon": tags.get("styl_sezon"),
                            "opis_dla_vto": tags.get("opis_dla_vto"),
                            "image_path": image_url 
                        })
                        
                        st.toast(f"✅ Dodano pomyślnie {uploaded_file.name}! Wykryto: {tags.get('typ_szczegolowy')}")
                        
                    except Exception as e:
                        st.error(f"Błąd podczas analizy/zapisu {uploaded_file.name}: {e}")

            st.success("Wszystkie zdjęcia zostały głęboko przeanalizowane i dodane bezpiecznie do chmury! Odświeżam szafę...")
            time.sleep(1.5)
            st.rerun()

# ==========================================
# ZAKŁADKA 3: DOBIERZ I WIZUALIZUJ
# ==========================================
with tab_dobierz:
    st.subheader("Wirtualny Stylista i Wizualizacja")
    
    st.markdown("#### 1. Zdjęcie bazowe Magdy")
    
    if os.path.exists(BASE_IMG_PATH):
        st.success("✅ Zdjęcie bazowe jest wczytane. System będzie go używał do każdego kolarzu.")
        col_b1, col_b2 = st.columns([1, 4])
        with col_b1:
            st.image(BASE_IMG_PATH, width=150)
        with col_b2:
            if st.button("🗑️ Zmień zdjęcie sylwetki"):
                os.remove(BASE_IMG_PATH)
                st.rerun()
    else:
        st.warning("⚠️ Najpierw wgraj swoje stałe zdjęcie referencyjne (twarz + sylwetka). Zrobić to musisz tylko raz!")
        base_uploader = st.file_uploader("Wgraj zdjęcie:", type=["jpg", "png", "jpeg"])
        if base_uploader:
            with open(BASE_IMG_PATH, "wb") as f:
                f.write(base_uploader.getbuffer())
            st.success("Zapisano! Odświeżam...")
            time.sleep(1)
            st.rerun()
    
    if os.path.exists(BASE_IMG_PATH):
        st.markdown("#### 2. Dobór ubrań z szafy")
        okazja = st.text_input("Opisz okazję (np. biuro, 15 stopni, chcę ubrać coś skórzanego):")
        
        if okazja:
            if st.button("✨ Dobierz zestaw z bazy", type="primary"):
                with st.spinner("AI wybiera ubrania..."):
                    items_ref = db.collection("wardrobe_items").stream()
                    wardrobe_data = [{"id": doc.id, **doc.to_dict()} for doc in items_ref]
                    
                    if not wardrobe_data:
                        st.error("Szafa jest pusta!")
                    else:
                        model_text = GenerativeModel("gemini-2.5-pro")
                        prompt_wybor = f"""
                        Masz następującą szafę (JSON metadane): {json.dumps(wardrobe_data)}
                        Okazja zgłoszona przez użytkownika: {okazja}.
                        Twoim zadaniem jest skomponowanie najlepszego zestawu ubrań na tę okazję. Zestaw powinien zawierać 2-4 logicznie pasujące elementy (np. góra, dół, buty).
                        Zwróć WYŁĄCZNIE czysty JSON (lista ID wybranych ubrań), np: ["id1", "id2", "id3"]
                        """
                        resp = model_text.generate_content(prompt_wybor)
                        try:
                            selected_ids = json.loads(resp.text.replace("```json", "").replace("```", "").strip())
                            st.session_state.selected_items = [i for i in wardrobe_data if i['id'] in selected_ids]
                            st.success("Zestaw dobrany!")
                        except:
                            st.error("Błąd w parsowaniu wyboru AI.")

        if "selected_items" in st.session_state and st.session_state.selected_items:
            st.markdown("#### Wybrany zestaw:")
            cols = st.columns(len(st.session_state.selected_items))
            
            vto_descriptions = ""
            valid_images = []
            
            try:
                base_img_pil = Image.open(BASE_IMG_PATH).convert("RGB")
                valid_images.append(base_img_pil)
            except Exception as e:
                st.error(f"Nie mogłem wczytać zdjęcia bazowego z dysku: {e}")

            for i, item in enumerate(st.session_state.selected_items):
                with cols[i]:
                    image_url = item.get("image_path", "")
                    if image_url.startswith("http"):
                        st.image(image_url, width=150)
                        try:
                            response = requests.get(image_url)
                            img_pil = Image.open(io.BytesIO(response.content)).convert("RGB")
                            valid_images.append(img_pil)
                        except Exception as e:
                            st.error("Błąd pobierania zdjęcia z chmury.")
                    st.caption(f"{item.get('typ_szczegolowy', '')}")
                vto_descriptions += f"- {item.get('opis_dla_vto', item.get('typ_szczegolowy', ''))}\n"
                    
            st.markdown("---")
            st.markdown("#### 3. Kolarz i Eksport do Gemini")
            st.info("Pobierz ten kolarz, skopiuj prompt i wklej je prosto do czatu Gemini!")

            if valid_images:
                target_height = 400
                resized_images = []
                for img in valid_images:
                    aspect_ratio = img.width / img.height
                    new_width = int(target_height * aspect_ratio)
                    resized_images.append(img.resize((new_width, target_height)))

                total_width = sum(img.width for img in resized_images) + (20 * (len(resized_images) - 1))
                collage = Image.new('RGB', (total_width, target_height), color=(255, 255, 255))
                
                x_offset = 0
                draw = ImageDraw.Draw(collage)
                
                for idx, img in enumerate(resized_images):
                    collage.paste(img, (x_offset, 0))
                    x_offset += img.width + 20
                    
                    if idx == 0 and len(resized_images) > 1:
                        line_x = x_offset - 10
                        draw.line([(line_x, 0), (line_x, target_height)], fill="black", width=6)
                
                st.image(collage, caption="Twój gotowy zestaw ze zdjęciem bazowym (przedzielony linią)", use_container_width=True)
                
                buf = io.BytesIO()
                collage.save(buf, format="JPEG")
                byte_im = buf.getvalue()
                
                st.download_button(
                    label="📥 Pobierz Gotowy Kolarz",
                    data=byte_im,
                    file_name="moj_zestaw_na_dzis.jpg",
                    mime="image/jpeg",
                    type="primary"
                )
                
                gemini_prompt = f"""Hej Gemini! Wygeneruj mi wizualizację typu Virtual Try-On.
Wgrywam Ci jeden zbiorczy obraz. Po lewej stronie (oddzielone grubą, czarną linią) znajduje się moje zdjęcie bazowe (twarz i sylwetka), a po prawej ubrania, które chcę na siebie założyć.

Proszę, użyj swojego modelu graficznego (Nano Banana 2 / Flash Image), aby ubrać mnie dokładnie w te rzeczy z kolarzu. Zachowaj moją twarz, fryzurę i proporcje ciała z lewej strony zdjęcia. Zwróć maksymalną uwagę na te kluczowe detale ubrań z prawej strony:
{vto_descriptions}

Obraz ma wyglądać jak profesjonalne zdjęcie w naturalnym otoczeniu, bez zniekształceń proporcji ubrań."""

                st.code(gemini_prompt, language="text")
