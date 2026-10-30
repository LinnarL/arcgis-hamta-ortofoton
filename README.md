# Hämta ortofoton

Python-verktygslåda för ArcGIS Pro som hämtar Lantmäteriets ortofoton för ett område och lägger
dem i kartan, så att de går att använda utan uppkoppling. Verktyget visar vilka år och bildtyper
som täcker området, och du väljer vad som ska hämtas och med vilken cellstorlek. Varje val blir
ett eget raster.

Bara området läses, direkt ur Lantmäteriets molnoptimerade filer. Hela rutor (upp till ungefär
700 MB styck) laddas aldrig ned, och vid en grövre cellstorlek läses bara filernas översikter.
För ett område på 16 ha över två rutor lästes ungefär 49 MB med 0,16 m och 4 MB med 2 m.

## Krav

- ArcGIS Pro 3.x, det räcker med en Basic-licens. Utvecklat och testat på 3.6 med Python 3.13.
- Inga extra paket. GDAL:s Python-bindningar och `certifi` ingår i standardmiljön `arcgispro-py3`.
- En consumer key och secret från Lantmäteriets API-portal med **både**:
  - API:et `STAC-bild`, och
  - en beställning av [Ortofoto Nedladdning](https://www.lantmateriet.se/sv/geodata/vara-produkter/produktlista/ortofoto-nedladdning/)
    på Geotorget. Produkten är avgiftsfri, men beställningen prövas och särskilda
    användningsvillkor måste godkännas.

  Det behövs ingen nyckel för att söka i katalogen, bara för att hämta.

## Installation

1. Klona eller ladda ned det här repot. Behåll `HamtaOrtofoton.pyt` och `ortofoto_worker.py` i
   samma mapp, eftersom verktygslådan kör arbetsfilen i separata processer.
2. I ArcGIS Pro: Catalog, Toolboxes, Add Toolbox, välj `HamtaOrtofoton.pyt`.
3. Öppna Lantmäteriet: hämta ortofoton, Hämta ortofoton.

## Användning

1. Välj område, som polygoner eller en utbredning.
2. Markera **Sök tillgängliga ortofoton**. Listan **Ortofoton att hämta** fylls med det som täcker
   området, till exempel `2025 | IR | 0,16 m | täcker 100 % | ca 15 MB`.
3. Kryssa i de rader du vill ha, ange en cellstorlek om du inte vill ha originalet, och kör.

## Parametrar

| Parameter | Standard | Förklaring |
|---|---|---|
| Avgränsa området med | Polygoner | Polygoner ur ett lager eller ritade i kartan, eller en utbredning |
| Intresseområde (polygoner) | tomt | Valfritt koordinatsystem. Alla objekt eller lagrets urval slås ihop, och rastren klipps till polygonerna |
| Utbredning | - | Rektangel: kartvyns utbredning, ett lagers utbredning, en ritad rektangel eller inskrivna koordinater. Inskrivna koordinater tolkas i den aktiva kartans koordinatsystem |
| Sök tillgängliga ortofoton | av | Anropar den öppna katalogen från dialogen, och gör om sökningen när området ändras. Av som standard, så att dialogen inte går ut på nätet förrän du ber om det |
| Ortofoton att hämta | - | Ett raster per rad. I ett skript anges de tre första fälten: `2025 \| Färg \| 0,16 m`. En okänd rad ger ett fel som listar det som finns |
| Utdata-arbetsyta | projektets geodatabas | Geodatabas eller mapp. I en mapp blir det GeoTIFF |
| Namnprefix | `ortofoto` | Utdatanamnen blir `<prefix>_<år>_<typ>`. Upplösningen läggs till om samma år och typ finns i två upplösningar |
| Cellstorlek (m) | filernas egen | 0,1 till 100 m. Grövre än filerna läser bara översikterna, finare ger ingen ny information |
| Consumer key / secret | - | Under Inloggning. Secret är ett dolt fält |
| Spara nyckeln på den här datorn | av | Under Inloggning. Sparar key och secret i Windows Autentiseringshanteraren när Lantmäteriet har godkänt dem. Sedan kan båda fälten lämnas tomma, och samma post används av alla verktygslådor här som tar den här nyckeln. Ta bort den under Autentiseringshanteraren, Windows-autentiseringsuppgifter |

Verktyget följer miljöinställningarna Output Coordinate System (standard SWEREF 99 TM), Cell Size
när det är ett tal, Snap Raster när den har samma koordinatsystem som utdata, Scratch Workspace
och att skriva över utdata.

## Bildtyper

| Typ | Band | Finns |
|---|---|---|
| Färg | rött, grönt, blått | från 2005 |
| IR | nära infrarött, rött, grönt (falsk färg) | från 2006 |
| Färg + IR | rött, grönt, blått, nära infrarött | från 2019 |
| Svartvit | ett gråskaleband | till ungefär 2004 |

Från 2019 levererar Lantmäteriet en fil med fyra band per ruta, 0,16 eller 0,4 m. Före det är färg
och IR separata filer med 0,25 eller 0,5 m. Filerna ligger i SWEREF 99 TM eller, för äldre bilder,
RT 90. Bilder i RT 90 projiceras om.

## Bra att veta

- Ett år täcker sällan hela landet, eftersom varje flygområde fotograferas med några års mellanrum.
  Täcker ett år bara en del av området varnar dialogen, loggen visar hur stor del, och resten blir
  NoData.
- Om två rutor på samma rad överlappar hamnar det nyaste fotot överst.
- Pixelvärdet 0 är NoData i utdata. Riktiga nollor i bilden höjs till 1, så att en mörk pixel aldrig
  blir ett hål.
- Vid originalets cellstorlek i SWEREF 99 TM kopieras pixlarna oförändrade. Grövre celler
  medelvärdesbildas, och finare celler eller ett annat koordinatsystem använder kubisk omsampling.
- Källa: Lantmäteriet. Den skrivs till varje rasters metadata. Läs användningsvillkoren för din
  beställning för hur bilderna får användas och hur källan ska anges.
