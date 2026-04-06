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
                    st.image(item["image_path"], use_container_width=True)
                
                st.caption(f"**Typ:** {item.get('typ_szczegolowy', 'Nieznany')}")
                st.caption(f"**Wygląd:** {item.get('kolor_wzor', '')}, {item.get('material_faktura', '')}")
                st.caption(f"**Detale:** {item.get('detale', '')}")
                
                with st.expander("🛠️ Zobacz ukryty prompt"):
                    st.code(item.get('opis_dla_vto', 'Brak szczegółowego opisu.'))
                
                if st.button("🗑️ Usuń", key=f"del_{item['id']}"):
                    db.collection("wardrobe_items").document(item['id']).delete()
                    if os.path.exists(item.get("image_path", "")):
                        os.remove(item["image_path"])
                    st.rerun()
                st.divider()

# ==========================================
# ZAKŁADKA 2: DODAJ (Auto-Tagger BOGATY O DETALE)
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
                        local_path = os.path.join(IMAGE_DIR, f"{file_id}.{file_ext}")
                        
                        with open(local_path, "wb") as f:
                            f.write(uploaded_file.getbuffer())
                            
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
                            "image_path": local_path
                        })
                        
                        st.toast(f"✅ Dodano pomyślnie {uploaded_file.name}! Wykryto: {tags.get('typ_szczegolowy')}")
                        
                    except Exception as e:
                        st.error(f"Błąd podczas analizy/zapisu {uploaded_file.name}: {e}")

            st.success("Wszystkie zdjęcia zostały głęboko przeanalizowane i dodane! Odświeżam szafę...")
            time.sleep(1.5)
            st.rerun()

        st.markdown("#### Podgląd wybranych zdjęć:")
        cols = st.columns(4)
        for i, file in enumerate(uploaded_files):
            with cols[i % 4]:
                st.image(file, caption=file.name, use_container_width=True)

# ==========================================
# ZAKŁADKA 3: DOBIERZ I WIZUALIZUJ (HYBRYDA Z GEMINI CHAT)
# ==========================================
with tab_dobierz:
    st.subheader("Wirtualny Stylista i Wizualizacja")
    
    st.markdown("#### 1. Zdjęcie bazowe Magdy")
    base_image = st.file_uploader("Wgraj zdjęcie Magdy (sylwetka)", type=["jpg", "png", "jpeg"], key="base_img")
    
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
        
        # --- ZMIANA: Twarde wczytywanie zdjęcia bazowego Magdy jako pierwszego obrazu ---
        if base_image is not None:
            try:
                base_image.seek(0) # Resetujemy wskaźnik strumienia
                img_bytes = base_image.read()
                base_img_pil = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                valid_images.append(base_img_pil)
            except Exception as e:
                st.warning(f"Wystąpił błąd podczas ładowania zdjęcia bazowego: {e}")

        # Zbieranie obrazów ubrań do kolarzu i wyświetlanie ich w UI
        for i, item in enumerate(st.session_state.selected_items):
            with cols[i]:
                if os.path.exists(item.get("image_path", "")):
                    st.image(item["image_path"], width=150)
                    valid_images.append(Image.open(item["image_path"]).convert("RGB"))
                st.caption(f"{item.get('typ_szczegolowy', '')}")
            vto_descriptions += f"- {item.get('opis_dla_vto', item.get('typ_szczegolowy', ''))}\n"
                
        st.markdown("---")
        st.markdown("#### 3. Kolarz i Eksport do Gemini")
        st.info("Pobierz ten kolarz, skopiuj prompt i wklej je prosto do czatu Gemini!")

        # Sklejanie obrazów w kolarz (Pillow)
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
                
                # Dodajemy pionową, czarną linię oddzielającą zdjęcie sylwetki od ubrań
                if idx == 0 and base_image is not None and len(resized_images) > 1:
                    line_x = x_offset - 10
                    draw.line([(line_x, 0), (line_x, target_height)], fill="black", width=4)
            
            # Wyświetlanie kolarzu w UI
            st.image(collage, caption="Twój gotowy zestaw ze zdjęciem bazowym (przedzielony linią)", use_container_width=True)
            
            # Przekształcanie kolarzu na bajty do pobrania
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
            
            st.markdown("##### 📋 Gotowy Prompt dla Gemini (Skopiuj to):")
            
            gemini_prompt = f"""Hej Gemini! Wygeneruj mi wizualizację typu Virtual Try-On.
Wgrywam Ci jeden zbiorczy obraz. Po lewej stronie (oddzielone czarną linią) znajduje się moje zdjęcie bazowe (twarz i sylwetka), a po prawej ubrania, które chcę na siebie założyć.

Proszę, użyj swojego modelu graficznego (Nano Banana 2 / Flash Image), aby ubrać mnie dokładnie w te rzeczy z kolarzu. Zachowaj moją twarz, fryzurę i proporcje. Zwróć maksymalną uwagę na te kluczowe detale ubrań:
{vto_descriptions}

Obraz ma wyglądać jak profesjonalne zdjęcie w naturalnym otoczeniu, bez zniekształceń proporcji ubrań."""

            st.code(gemini_prompt, language="text")
