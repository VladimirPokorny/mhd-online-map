# Mapa veřejné dopravy IDS JMK

Webová aplikace, která nad mapou OpenStreetMap zobrazuje síť Integrovaného dopravního systému Jihomoravského kraje. Data bere z jízdního řádu ve formátu GTFS, který zveřejňuje KORDIS JMK ([data.brno.cz](https://data.brno.cz/datasets/379d2e9a7907460c8ca7fda1f3e84328/about)). Nové verze si aplikace stahuje sama.

- **Linky** jsou vykreslené po skutečné trase (koleje, silnice), ne rovnými čarami mezi zastávkami.
- **Vozidla** se ukazují v libovolném čase zvoleného dne: pracovní den, sobota, neděle i svátek.
- **Graf** ukazuje počet vozidel v provozu během celého dne podle druhu dopravy.
- **Mapa dostupnosti** ukáže, kam se z vybrané zastávky dostanete za 15–120 minut.

---

## 1. Návod k ovládání

### Levý panel – linky a filtry

| Prvek | Co dělá |
|---|---|
| **Hledat číslo linky, cíl nebo zastávku** | Filtruje linky, třeba `1`, `S2`, `Blansko` nebo `Mendlovo náměstí`. Filtr platí pro mapu, seznam i vozidla. |
| **Tramvaj / Trolejbus / Autobus / Vlak / Loď** | Zapíná a vypíná jednotlivé druhy dopravy. |
| **Vozidla / Linky / Zastávky** | Zobrazí nebo skryje vrstvu na mapě. Zastávky se ukážou až po přiblížení. |
| **Seznam linek** | Kliknutím linku vyberete. Mapa se na ni přiblíží, ostatní linky ztlumí a dole se objeví detail: počet spojů, zastávky a počet právě jedoucích vozidel. Kliknutím na název zastávky v detailu se na ni mapa přesune. |

Linku vyberete i kliknutím na mapě, na trasu nebo na vozidlo. Výběr zrušíte kliknutím do prázdného místa mapy nebo křížkem v detailu.

### Časová osa (pod mapou)

1. **Datum:** vyberte konkrétní den v platnosti jízdního řádu, nebo klikněte na **Pracovní den / Sobota / Neděle / Svátek**. Aplikace pak vybere nejbližší takový den. Vedle se ukáže, o jaký den jde, u svátku i jeho název (svátky jedou podle nedělního jízdního řádu).
2. **Čas:** posuňte jezdec, nebo klikněte či táhněte přímo v grafu.
   - Klávesy **← / →** posunou čas o 1 minutu, se **Shiftem** o 10 minut.
3. **Přehrávání:** tlačítko **▶** (nebo mezerník) spustí animaci dne. Rychlost nastavíte vedle (1× až 900×).
4. **Graf:** skládaný graf počtu vozidel v provozu během dne podle druhu dopravy. Po najetí myší ukáže přesné počty, svislá čára značí zvolený čas.
5. **Počty nad grafem:** kolik vozidel kterého druhu je v provozu v daném okamžiku v celé síti. Velké číslo vpravo je počet vozidel na mapě (po případném filtrování).

**Vozidla na mapě** jsou tečky v barvě linky. Po najetí myší uvidíte linku, cíl a další zastávku. Kliknutím vyberete linku.

### Kam se dostanu ze zastávky? (mapa dostupnosti)

1. Do pole **„Kam se dostanu ze zastávky?“** napište zastávku, třeba `Hlavní nádraží`. Druhá možnost: klikněte na zastávku na mapě a zvolte **„Kam se odsud dostanu?“**.
2. Vyberte časový limit (15–120 min).
3. Odjezd se řídí **datem a časem na časové ose**. Když čas změníte nebo spustíte přehrávání, mapa se přepočítá.

Jak číst mapu:

| Barva | Význam |
|---|---|
| **Modrý přechod** (tmavá → světlejší) | Kam se dostanete do zvoleného limitu. Čím tmavší, tím dřív tam budete. |
| **Světle šedá** | Dosažitelné, ale až po limitu (do dvojnásobku limitu, nejvýš 4 hodiny). |
| **Tmavě šedá** | Dál, než se v tomto čase dostanete. |

- Barva každého místa je **nejrychlejší kombinace jízdy a chůze** (z poslední zastávky se počítá chůze).
- Po přiblížení se ukážou dosažitelné zastávky. Po najetí myší uvidíte čas cesty, po kliknutí **celé spojení**, například *„S2 8:29 Hlavní nádraží → 8:59 Blansko město“*.
- Ze zastávky ve výsledku můžete spustit nový výpočet.
- Mapa dostupnosti vypne vrstvu vozidel, aby nepřekážela. Zapnete ji zaškrtnutím „Vozidla“. Křížkem se vše vrátí zpět.

### Odkazy na konkrétní pohled

Adresu můžete uložit nebo poslat:

- `#15/49.1960/16.6100` – přiblížení a střed mapy (aktualizuje se samo při posouvání),
- `?from=Hlavní nádraží&date=2026-10-14&time=08:00&minutes=60` – rovnou otevře mapu dostupnosti.

---

## 2. Jak to funguje

### Data

| Zdroj | Použití |
|---|---|
| Jízdní řád IDS JMK (GTFS) – <https://kordis-jmk.cz/gtfs/gtfs.zip> | linky, zastávky, spoje, kalendář provozu, přestupy |
| OpenStreetMap (Overpass API) | skutečné trasy linek (relace sítě IDS JMK), kolejová síť |
| OSRM (veřejný směrovací server) | dopočet trasy autobusů, které v OSM nejsou zmapované |
| Dlaždice OpenStreetMap | podkladová mapa v prohlížeči |

Data z OSM a OSRM se ukládají do `osm_cache/` a stahují se znovu jen tehdy, když je nový jízdní řád potřebuje (třeba nová linka).

### Automatická aktualizace jízdního řádu

KORDIS zveřejňuje nový jízdní řád jednou týdně (v neděli). Aplikace to řeší sama:

1. **Kontrola:** každých 6 hodin (nastavitelné) se zeptá serveru, jestli se `gtfs.zip` změnil. Používá podmíněný dotaz (`ETag` / `Last-Modified`), takže když se nic nezměnilo, nic se nestahuje.
2. **Nová verze:** stáhne se (asi 8 MB), ověří se a rozbalí do vlastní složky `feeds/<verze>/`.
3. **Zpracování na pozadí:** aplikace po celou dobu dál obsluhuje původní verzi a přepne se, až je nová připravená.
4. **Bezpečnost:** jako aktuální se nová verze potvrdí až po úspěšném zpracování. Když selže (třeba je nedostupný OpenStreetMap), běží dál původní data a při další kontrole se to zkusí znovu. Uchovávají se poslední 2 verze.
5. **Otevřené stránky:** prohlížeče s otevřenou starší verzí ukážou lištu *„Jízdní řád byl aktualizován – Načíst znovu“*. Po načtení zůstane zachované datum, čas i vybraná zastávka.

V hlavičce aplikace je vidět, odkdy dokdy jízdní řád platí, kdy byl zveřejněn a kdy proběhla poslední kontrola.

### Trasy linek

GTFS od IDS JMK neobsahuje soubor `shapes.txt` (tvary tras). Trasy se proto skládají takto:

1. **Linka zmapovaná v OSM** se spáruje podle čísla a druhu dopravy (293 linek). Trasa se použije, jen pokud leží aspoň 80 % zastávek linky do 200 m od ní.
2. **Ostatní linky** se dopočítají přes zastávky v pořadí jízdy (58 linek): autobusy a trolejbusy přes OSRM, tramvaje a vlaky nejkratší cestou po kolejích z OSM.
3. **Rovná čára** mezi zastávkami se použije jen jako poslední možnost.

Jak byla trasa získána, uvidíte v detailu linky.

### Polohy vozidel

- Každý spoj, který v daný okamžik jede, je jedno vozidlo.
- Poloha se počítá z jízdního řádu: mezi odjezdem a příjezdem se vozidlo plynule posouvá po trase, v zastávce stojí po dobu pobytu.
- Započítávají se i spoje, které vyjely předchozí den a jedou přes půlnoc.
- `departure_time` na konečné zastávce znamená v těchto datech odjezd na další spoj (čekání na konečné), takže se nepočítá jako jízda.
- Jde o **polohy podle jízdního řádu**, ne o živá data. Zpoždění se nezohledňuje.

### Mapa dostupnosti

- **Výpočet:** používá algoritmus *Connection Scan* nad jízdním řádem zvoleného dne. Najde nejdřívější příjezd do každé zastávky. Jeden výpočet trvá asi 0,1 s.
- **Přestupy:**
  - na stejné zastávce aspoň 1 minuta
  - oficiální pěší přestupy ze souboru `transfers.txt`
  - chůze do 400 m mezi blízkými zastávkami (asi 4,5 km/h)
- **Výchozí bod:** všechna stanoviště se stejným názvem jako vybraná zastávka.
- **Spojení:** při stejném čase příjezdu má přednost to s menším počtem přestupů.
- **Plocha mapy:** z každé dosažené zastávky se čas šíří pěší rychlostí do okolí. Síť buněk má velikost asi 160 m a pokrývá celý kraj.

### Omezení

- Bez živých dat o zpoždění a bez návazností vázaných na konkrétní spoje. KORDIS zveřejňuje i GTFS Realtime (`https://kordis-jmk.cz/gtfs/gtfsReal.dat`), takže zpoždění by šlo doplnit.
- Chůze se počítá vzdušnou čarou s přirážkou na zacházku, ne po skutečných cestách.
- Asi 5 % úseků mezi zastávkami nejde napasovat na trasu linky (hlavně zastávky regionálních autobusů mimo trasu z OSM). Vozidlo tam jede rovně.

---

## 3. Spuštění na vlastním počítači

Je potřeba Python 3.12 nebo novější.

```bash
pip install -r requirements.txt
python app.py
```

Pak otevřete <http://127.0.0.1:5000>.

- **Odkud se berou data:** při startu se použije poslední stažená verze jízdního řádu, nebo složka `GTFS/`, pokud existuje. Když není ani jedno, aplikace jízdní řád nejdřív stáhne.
- **Aktualizace:** běží na pozadí podle odstavce *Automatická aktualizace jízdního řádu*.

Parametry:

```bash
python app.py --update                 # před startem stáhne nejnovější jízdní řád
python app.py --update --build-only    # jen stáhne a zpracuje, pak skončí
python app.py --cache ./data --port 8000 --host 0.0.0.0
python app.py --url ""                 # bez stahování, jen data ze složky GTFS/
```

- **Start:** s hotovou cache trvá asi 2–3 s, zpracování nové verze asi 10–15 s.
- **První spuštění bez `osm_cache/`:** stahuje data z OSM a OSRM a trvá asi 7 minut.

---

## 4. Docker

```bash
docker build -t idsjmk-map .
docker run -p 8000:8000 -v idsjmk-data:/app/data idsjmk-map
```

Pak otevřete <http://localhost:8000>.

Co dělá image:

- **Při sestavení** stáhne aktuální jízdní řád od KORDIS a předpočítá trasy z přiložené `osm_cache/` (`python app.py --update --build-only`, asi 15 s). Sestavení proto potřebuje přístup na internet. Kontejner pak startuje za 2–3 s.
- **Za běhu** kontroluje nové verze jízdního řádu a nasazuje je bez restartu.
- Používá produkční server **gunicorn**: 1 proces a 8 vláken. Celý jízdní řád drží v paměti, kontejner potřebuje asi 400 MB RAM, při přepínání na novou verzi krátce až zhruba dvojnásobek.
- **Health check** je na `GET /healthz`. Vrací i verzi dat a informace o zdroji.
- Běží pod neprivilegovaným uživatelem `app`.
- Složka `GTFS/` se do image nekopíruje (je v `.dockerignore`).

**Svazek `/app/data` je doporučený.** Uchová stažené verze jízdního řádu a cache i přes restart a nové nasazení. Bez něj kontejner po restartu začne verzí z doby sestavení image a novou verzi si stáhne znovu. Pojmenovaný svazek se při prvním použití naplní obsahem z image.

Nastavení přes proměnné prostředí:

| Proměnná | Výchozí | Význam |
|---|---|---|
| `GTFS_URL` | `https://kordis-jmk.cz/gtfs/gtfs.zip` | odkud stahovat jízdní řád; prázdná hodnota aktualizace vypne |
| `GTFS_UPDATE_INTERVAL_HOURS` | `6` | jak často kontrolovat novou verzi |
| `CACHE_DIR` | `/app/data` | stažené verze (`feeds/`, `feed.json`), cache tras (`network_cache.json`, `osm_cache/`) |
| `GTFS_DIR` | `/app/GTFS` | volitelný přibalený jízdní řád (záloha, když se stahování nepovede) |
| `PORT` | `8000` | port uvnitř kontejneru |

Je přiložený i `docker-compose.yml` (`docker compose up --build`), který svazek nastavuje.

---

## 5. Nasazení na Dokploy

### Příprava repozitáře

Dokploy sestavuje aplikaci z Git repozitáře. Do repozitáře **patří složka `osm_cache/`** (asi 35 MB). Bez ní by se při každém sestavení znovu stahovala data z OSM, což trvá několik minut a zatěžuje veřejné servery. Složka `GTFS/` potřeba není, jízdní řád se stahuje automaticky.

```bash
git init
git add .
git commit -m "Mapa IDS JMK"
git remote add origin git@github.com:<uzivatel>/idsjmk-map.git
git push -u origin main
```

`network_cache.json`, `feeds/` a `feed.json` se do Gitu nedávají (jsou v `.gitignore`), vytvoří se samy.

### Varianta A – Application (Dockerfile), doporučeno

1. V Dokploy: **Projects → Create Project**, potom **Create Service → Application**.
2. **Provider:** GitHub / GitLab / Git. Vyberte repozitář a větev `main`.
3. **Build Type:** `Dockerfile`, Docker File: `./Dockerfile`, Docker Context Path: `.`
4. **Deploy:** první sestavení trvá asi 1–2 minuty.
5. **Domains → Add Domain:**
   - Host: vaše doména (např. `mapa.example.cz`), Path `/`
   - **Container Port: `8000`**
   - HTTPS zapnuté, certifikát *Let's Encrypt*
6. **Advanced → Volumes:** přidejte *Volume Mount* (pojmenovaný svazek, např. `idsjmk-data`) s cestou v kontejneru **`/app/data`**.
7. Doporučené prostředky (**Advanced → Resources**): limit paměti aspoň **1 GB** (kvůli přepínání verzí).
8. Volitelně v **Environment** nastavte `GTFS_UPDATE_INTERVAL_HOURS` (výchozí 6).

### Varianta B – Docker Compose

1. **Create Service → Compose**, stejný repozitář, Compose Path `./docker-compose.yml`.
2. V **Domains** zvolte službu `idsjmk-map` a port `8000`.

### Po nasazení

- Ověřte `https://<domena>/healthz`. Odpověď obsahuje `"status": "ok"`, verzi dat a v části `feed` datum zveřejnění a poslední kontroly.
- V **Logs** uvidíte řádek `Loaded 347 lines, ... (version …, download feed)`. Při každé kontrole se objeví `GTFS feed unchanged`, nebo `New GTFS feed downloaded …`.
- Automatické nasazování po `git push` zapnete v Dokploy přes **Auto Deploy** (webhook).

---

## 6. Aktualizace jízdního řádu

Jízdní řád se aktualizuje **automaticky** (viz *Automatická aktualizace jízdního řádu*). Nic dělat nemusíte.

Ručně můžete:

- **vynutit kontrolu hned:** restartujte službu. Kontrola proběhne 5 s po startu.
- **použít jiný zdroj nebo vlastní export:** nastavte `GTFS_URL`, nebo aktualizace vypněte (`GTFS_URL=`) a jízdní řád dejte do složky `GTFS/` (pak ji ale odeberte z `.dockerignore`).

Když nový jízdní řád obsahuje nové linky, aplikace si při zpracování dotáhne chybějící trasy z OSM nebo OSRM a uloží je do `osm_cache/` ve svazku. Do repozitáře je občas můžete přenést lokálně:

```bash
python app.py --update --build-only
git add osm_cache && git commit -m "Aktualizace osm_cache"
```

Aktuální trasy z OpenStreetMap stáhnete takto (asi 7 minut, s internetem):

```bash
rm -rf osm_cache network_cache.json
python app.py --update --build-only
```

---

## 7. Struktura projektu

| Soubor | Obsah |
|---|---|
| `app.py` | Flask server, API, spouštění (vývoj i gunicorn), aktualizace za chodu |
| `gtfs_update.py` | stahování a správa verzí jízdního řádu |
| `gtfs_loader.py` | načtení GTFS, sestavení sítě a cache |
| `osm_tracks.py` | trasy z OpenStreetMap, dopočet přes OSRM a koleje, úseky mezi zastávkami |
| `timetable.py` | kalendář provozu, svátky, polohy vozidel, denní profil |
| `reach.py` | mapa dostupnosti (Connection Scan, mřížka časů) |
| `templates/index.html` | celé webové rozhraní (Leaflet) |
| `GTFS/` | volitelný přibalený jízdní řád (záloha) |
| `osm_cache/` | stažená data z OSM a OSRM |
| `feeds/`, `feed.json` | stažené verze jízdního řádu (vznikají za běhu v `CACHE_DIR`) |

### API

| Endpoint | Popis |
|---|---|
| `GET /api/network` | linky (s trasami) a zastávky |
| `GET /api/calendar` | platnost jízdního řádu a svátky |
| `GET /api/profile?date=YYYY-MM-DD` | počet vozidel v provozu po 5 minutách podle druhu dopravy |
| `GET /api/vehicles?date=YYYY-MM-DD&time=<sekundy od půlnoci>` | vozidla v daném okamžiku |
| `GET /api/reach?date=…&time=…&stop=<index zastávky>&minutes=60` | mapa dostupnosti |
| `GET /healthz` | kontrola stavu, verze dat a informace o zdroji |

Endpointy kromě `/api/network` přijímají parametr `v=<verze>`. Když server mezitím přešel na novější jízdní řád, odpoví `409` a stránka nabídne nové načtení.

---

## 8. Poznámky k veřejnému provozu

- **Mapové dlaždice** se načítají ze serverů OpenStreetMap (`tile.openstreetmap.org`). Ty jsou určené pro mírný provoz a vyžadují uvedení autorství (je v rohu mapy). Pro větší návštěvnost použijte komerčního poskytovatele dlaždic nebo vlastní server. Adresa dlaždic je v `templates/index.html` (`L.tileLayer(...)`).
- **Overpass a OSRM** se volají jen při zpracování nové verze jízdního řádu, a to pouze pro data, která chybí v `osm_cache/`. Při běžném provozu ne.
- **Stahování jízdního řádu** je jeden podmíněný dotaz každých 6 hodin. Celý soubor (8 MB) se stáhne jen tehdy, když se změnil.
- **Data jízdního řádu:** KORDIS JMK, DPMB. **Mapová data:** © přispěvatelé OpenStreetMap (licence ODbL).
