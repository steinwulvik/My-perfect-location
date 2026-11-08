# My Perfect Location

Lokal app for å undersøke ett sted eller sammenligne opptil tre adresser i Norge. Skriv minst tre tegn og velg en adresse fra listen. Ingen adresse eller kategorier er valgt ved oppstart.

## Oppstart

Python og pakken certifi kreves. Start fra appmappen:

```powershell
python -m pip install -r requirements.txt
python server.py
```

Åpne http://127.0.0.1:8003/.

## Render

Opprett tjenesten som en **Web Service** fra GitHub-repoet `steinwulvik/My-perfect-location`. Velg branch `main`, runtime `Python`, build command `pip install -r requirements.txt` og start command `python server.py`. Sett health check path til `/api/health`. Render leverer `PORT` ved oppstart; serveren bruker den automatisk og lytter på `0.0.0.0` i Render-miljøet. Lokal oppstart beholder adressen `127.0.0.1:8003`.

## Datakilder

- Kartverket: fortløpende adresseforslag. Nominatim: separat manuelt stedsoppslag i reserveflyten.
- OpenStreetMap/Overpass: nærtilbud, turstier/skiløyper og supplerende hytter. Entur: kollektivholdeplasser og kollektivruter.
- UT.no/DNT: hovedkilde for hyttekatalogen, med OpenStreetMap som reserve. Katalogen hentes fra `https://ut.no/api/graphql`, og `UT_GRAPHQL_URL` kan overstyre adressen.
- NVE: innsjøer, flomhendelser, kvikkleire og skredlag. NGU/DSA: radon. Kartverket: høydekontroll.
- OFA/Inatur, NINA/GBIF, Miljødirektoratet og lokale fiskekortkoblinger: opplysninger om vann, fisk og badeplasser. Wikipedia: artsinformasjon og bilder med kilde/lisens.
- Valhalla/FOSSGIS: gang-, sykkel- og bilruter. Leaflet og OpenStreetMap: kartvisning.
- DNTs bestillingsløsning og Inatur: eksterne lenker for henholdsvis hyttebestilling og fiskekort.

Felles nettjenester kan ha nedetid og begrenset dekning. Kartlagt fravær av treff er ikke dokumentasjon på at et tilbud eller en naturfare mangler. Fiskekort, fiskeregler og tilgang må bekreftes hos rettighetshaver. Temperatur og vannkvalitet vises som «ikke tilgjengelig» når appen mangler slike data. Appen viser ingen egen DNT-kalender eller garanti for ledige senger.

UT-katalogen mellomlagres i én time, hytter/nærtilbud i 15 minutter og vann i 30 minutter. Delvise kildefeil mellomlagres ikke som komplette resultater. Oversiktslag og enkle risikoregler erstatter ikke faglige vurderinger.

Fiskevann som er dokumentert i en kontrollert fiskekortkilde kan vises selv om GBIF mangler artsregistreringer. Artslisten blir da tom med en forklaring i grensesnittet. OFA-kilden dekker navngitte vann i Østmarka; område, kommune, navn og kjente navnevarianter kontrolleres mot NVE. Dette er fremdeles ikke et uttømmende nasjonalt register. Kjente drikkevannsutelukkelser beholdes.

## Finn steder som passer for deg

Velg kommune og deretter et sted fra nedtrekkslisten. Listen hentes fra Kartverkets stedsnavnregister (SSR), ikke poststedsregisteret. Byer/tettsteder/lokalområder vises først, deretter andre registrerte lokale stedsnavn. Alternative navn samles per stedsnummer; inaktive steder og overordnede fylkes-/landsnavn utelates. Steder uten gyldig kartpunkt vises deaktivert. Ved bytte av kommune forkastes gamle svar og stedvalget nullstilles.

Stedsnavnlisten viser kun unike byer, tettsteder, bydeler, grender, bygder og boligfelt fra Kartverket. Gater, adresser og enkeltobjekter som skoler og broer filtreres bort. Appen bruker en fast søkeavstand på 3 km rundt stedets kartpunkt og tar ikke med adresser utenfor valgt kommune. Avstanden er ikke en offisiell by-/bygdegrense. Søket henter alle sider fra adresse-API-et og vurderer tilgjengelige adresser i avgrensningen.

Ingen kriterier er valgt på forhånd. Må-krav filtrerer, ønsker rangerer. Hvis adressekilden når grensen på 10 000 treff, må et mer avgrenset stedsnavn velges. Manglende sider, koordinater eller duplikater hindrer komplett-status. Bare adresser som passer de valgte filtrene vises som husikoner på kartet. Grønt betyr at valgte ønsker også er oppfylt; oransje betyr at enkelte ønsker mangler eller er uavklarte. Statuslinjen oppgir kategoriene som hentes og vurderes, og viser hvor mange naturfareoppslag som pågår, er ferdige eller ble hoppet over fordi andre må-krav ikke var oppfylt. Husene samles med antall ved utzooming; klikk for innzooming, og klikk et hus for en hvit detaljboks. Boksen kan flyttes, minimeres, lukkes og lukkes igjen ved å klikke samme hus. Ingen adresse-/gateliste vises utenfor kartet; ferdigstatusen gir bare samlet treff- og dekningsinformasjon. Full adresseliste er ikke det samme som full dekning av tilbud eller naturfare.

Avstand til registrerte tilbud er i luftlinje, ikke skolekrets, ledig plass eller faktisk reisevei. Naturfare er kontroll av registrerte signaler, ikke garanti for risikofri eiendom. Fiskevann kan være uavklart på grunn av begrenset kildedekning. Søket kan stoppes og merkes da delvis.

Appen er testet med Promptfoo (32 testtilfeller).

### Kart og ytelse

Kartoverskriften er «Steder som passer til deg». Bakgrunnskartet tones ned til grått i denne visningen. Terrengsymboler i OpenStreetMaps ferdigtegnede kartbilder, som høydepunkter, tilhører bakgrunnen og er ikke appens treff eller naturfaremarkeringer; de kan ikke fjernes enkeltvis med CSS. Ingen alternativ betalings-/nøkkeltjeneste er tatt i bruk.

Nærtilbud og badeplasser grupperes én gang i et romlig rutenett. Vannpolygoner får forhåndsberegnede avgrensningsbokser. Beregninger gjenbrukes for adresser på samme koordinat i ett søk. Husmarkører bygges i små avbrytbare puljer og grupperes med Leaflet.markercluster 1.5.3, med overlappende punkter tilgjengelige ved maksimal innzooming. Både markører og åpne detaljbokser fjernes når søkevalgene endres.

Naturfare undersøkes bare når kategorien er valgt og andre må-krav er bekreftet. Statusen viser hvilke kategorier som faktisk hentes/vurderes, og beholder naturfarestatus mens et slikt oppslag fortsatt venter.

Servercachen bruker LRU, maksimalt 4096 oppføringer og omtrent 64 MiB målt objektstørrelse per cacheinnhold, med maks 8 MiB per enkeltoppføring. Dette er en begrensning på bufrede data, ikke en garanti for hele prosessens minnebruk. Eksisterende TTL-er beholdes; feil og delvise kildesvar lagres ikke som komplette resultater. Fiske-/badevannskilder som ikke er valgt i områdesøket hentes ikke.
