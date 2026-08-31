# NSE XBRL / Price Batches — Nifty 500 and USA

## Nifty 500 Batches (449 companies)

Nifty 500 constituents minus the 50 Nifty 50 companies (tracked/ingested
separately) and minus IDFCFIRSTB (already ingested). Batch size 10, same
fetch+ingest pipeline as Nifty 50 (quarterly + Q4-derived annual + balance
sheet, both statement types; real usable NSE XBRL data realistically starts
~mid-2022 regardless of the fetch window requested). Not started — fetching
has not begun on any of these; this section is the plan, execute on request.

**Scale reality**: at the pace observed for Nifty 50 (~3-4 min/company for
Quarterly+Annual fetch), 45 batches of ~10 is roughly 25-30 hours of
NSE fetch time alone, before any ingestion. Realistic to run unattended over
several days, not a single sitting.

### Batch 1 — pending (10)
360ONE, 3MINDIA, AADHARHFC, AARTIIND, AAVAS, ABB, ABBOTINDIA, ABCAPITAL, ABDL, ABFRL

### Batch 2 — pending (10)
ABLBL, ABREL, ABSLAMC, ACC, ACE, ACMESOLAR, ACUTAAS, ADANIENSOL, ADANIGREEN, ADANIPOWER

### Batch 3 — pending (10)
AEGISLOG, AEGISVOPAK, AFCONS, AFFLE, AIAENG, AIIL, AJANTPHARM, ALKEM, AMBER, AMBUJACEM

### Batch 4 — pending (10)
ANANDRATHI, ANANTRAJ, ANGELONE, ANTHEM, ANURAS, APARINDS, APLAPOLLO, APOLLOTYRE, APTUS, ARE&M

### Batch 5 — pending (10)
ASAHIINDIA, ASHOKLEY, ASTERDM, ASTRAL, ATGL, ATHERENERG, ATUL, AUBANK, AUROPHARMA, AWL

### Batch 6 — pending (10)
BAJAJHFL, BAJAJHLDNG, BALKRISIND, BALRAMCHIN, BANDHANBNK, BANKBARODA, BANKINDIA, BATAINDIA, BAYERCROP, BBTC

### Batch 7 — pending (10)
BDL, BELRISE, BEML, BERGEPAINT, BHARATFORG, BHARTIHEXA, BHEL, BIKAJI, BIOCON, BLS

### Batch 8 — pending (10)
BLUEDART, BLUEJET, BLUESTARCO, BOSCHLTD, BPCL, BRIGADE, BRITANNIA, BSE, BSOFT, CAMS

### Batch 9 — pending (10)
CANBK, CANFINHOME, CANHLIFE, CAPLIPOINT, CARBORUNIV, CARTRADE, CASTROLIND, CCL, CDSL, CEATLTD

### Batch 10 — pending (10)
CEMPRO, CENTRALBK, CESC, CGCL, CGPOWER, CHALET, CHAMBLFERT, CHENNPETRO, CHOICEIN, CHOLAFIN

### Batch 11 — pending (10)
CHOLAHLDNG, CIEINDIA, CLEAN, COCHINSHIP, COFORGE, COHANCE, COLPAL, CONCOR, CONCORDBIO, COROMANDEL

### Batch 12 — pending (10)
CPPLUS, CRAFTSMAN, CREDITACC, CRISIL, CROMPTON, CUB, CUMMINSIND, CYIENT, DABUR, DALBHARAT

### Batch 13 — pending (10)
DATAPATTNS, DCMSHRIRAM, DEEPAKFERT, DEEPAKNTR, DELHIVERY, DEVYANI, DIVISLAB, DIXON, DLF, DMART

### Batch 14 — pending (10)
DOMS, ECLERX, EIDPARRY, EIHOTEL, ELECON, ELGIEQUIP, EMAMILTD, EMCURE, EMMVEE, ENDURANCE

### Batch 15 — pending (10)
ENGINERSIN, ENRIN, ERIS, ESCORTS, EXIDEIND, FACT, FEDERALBNK, FINCABLES, FIRSTCRY, FIVESTAR

### Batch 16 — pending (10)
FLUOROCHEM, FORCEMOT, FORTIS, FSL, GABRIEL, GAIL, GALLANTT, GESHIP, GICRE, GILLETTE

### Batch 17 — pending (10)
GLAND, GLAXO, GLENMARK, GMDCLTD, GMRAIRPORT, GODFRYPHLP, GODIGIT, GODREJCP, GODREJIND, GODREJPROP

### Batch 18 — pending (10)
GPIL, GRANULES, GRAPHITE, GRAVITA, GROWW, GRSE, GVT&D, HAL, HAVELLS, HBLENGINE

### Batch 19 — pending (10)
HDBFS, HDFCAMC, HEG, HEROMOTOCO, HEXT, HFCL, HINDCOPPER, HINDPETRO, HINDZINC, HOMEFIRST

### Batch 20 — pending (10)
HONASA, HONAUT, HSCL, HUDCO, HYUNDAI, ICICIAMC, ICICIGI, ICICIPRULI, IDBI, IDEA

### Batch 21 — pending (9; IDFCFIRSTB already ingested, excluded)
IEX, IFCI, IGIL, IGL, IIFL, IKS, INDGN, INDHOTEL, INDIACEM

### Batch 22 — pending (10)
INDIAMART, INDIANB, INDUSINDBK, INDUSTOWER, INOXWIND, INTELLECT, IOB, IOC, IPCALAB, IRB

### Batch 23 — pending (10)
IRCON, IRCTC, IREDA, IRFC, ITCHOTELS, ITI, J&KBANK, JAINREC, JBMA, JINDALSAW

### Batch 24 — pending (10)
JINDALSTEL, JKCEMENT, JKTYRE, JMFINANCIL, JPPOWER, JSL, JSWCEMENT, JSWDULUX, JSWENERGY, JSWINFRA

### Batch 25 — pending (10)
JUBLFOOD, JUBLINGREA, JUBLPHARMA, JWL, JYOTICNC, KAJARIACER, KALYANKJIL, KARURVYSYA, KAYNES, KEC

### Batch 26 — pending (10)
KEI, KFINTECH, KIMS, KIRLOSENG, KPIL, KPITTECH, KPRMILL, LALPATHLAB, LATENTVIEW, LAURUSLABS

### Batch 27 — pending (10)
LEMONTREE, LENSKART, LGEINDIA, LICHSGFIN, LICI, LINDEINDIA, LLOYDSME, LODHA, LTF, LTFOODS

### Batch 28 — pending (10)
LTM, LTTS, LUPIN, M&MFIN, MAHABANK, MANAPPURAM, MANKIND, MAPMYINDIA, MARICO, MAZDOCK

### Batch 29 — pending (10)
MCX, MEDANTA, MEESHO, MFSL, MGL, MINDACORP, MMTC, MOTHERSON, MOTILALOFS, MPHASIS

### Batch 30 — pending (10)
MRF, MRPL, MSUMI, MUTHOOTFIN, NAMINDIA, NATCOPHARM, NATIONALUM, NAUKRI, NAVA, NAVINFLUOR

### Batch 31 — pending (10)
NBCC, NCC, NETWEB, NEULANDLAB, NEWGEN, NH, NHPC, NIACL, NIVABUPA, NLCINDIA

### Batch 32 — pending (10)
NMDC, NSLNISP, NTPCGREEN, NUVAMA, NUVOCO, NYKAA, OBEROIRLTY, OFSS, OIL, OLAELEC

### Batch 33 — pending (10)
OLECTRA, ONESOURCE, PAGEIND, PARADEEP, PATANJALI, PAYTM, PCBL, PERSISTENT, PETRONET, PFC

### Batch 34 — pending (10)
PFIZER, PFOCUS, PGEL, PHOENIXLTD, PIDILITIND, PIIND, PINELABS, PIRAMALFIN, PNB, PNBHOUSING

### Batch 35 — pending (10)
POLICYBZR, POLYCAB, POLYMED, POONAWALLA, POWERINDIA, PPLPHARMA, PREMIERENE, PRESTIGE, PTCIL, PVRINOX

### Batch 36 — pending (10)
PWL, RADICO, RAILTEL, RAINBOW, RAMCOCEM, RBLBANK, RECLTD, REDINGTON, RHIM, RITES

### Batch 37 — pending (10)
RKFORGE, RPOWER, RRKABEL, RVNL, SAGILITY, SAIL, SAILIFE, SAMMAANCAP, SAPPHIRE, SARDAEN

### Batch 38 — pending (10)
SAREGAMA, SBICARD, SCHAEFFLER, SCHNEIDER, SCI, SHREECEM, SHYAMMETL, SIEMENS, SIGNATURE, SJVN

### Batch 39 — pending (10)
SOBHA, SOLARINDS, SONACOMS, SONATSOFTW, SPLPETRO, SRF, STARHEALTH, SUMICHEM, SUNDARMFIN, SUNTV

### Batch 40 — pending (10)
SUPREMEIND, SUZLON, SWANCORP, SWIGGY, SYNGENE, SYRMA, TARIL, TATACAP, TATACHEM, TATACOMM

### Batch 41 — pending (10)
TATAELXSI, TATAINVEST, TATAPOWER, TATATECH, TBOTEK, TECHNOE, TEGA, TEJASNET, TENNIND, THELEELA

### Batch 42 — pending (10)
THERMAX, TIINDIA, TIMKEN, TITAGARH, TMCV, TORNTPHARM, TORNTPOWER, TRAVELFOOD, TRIDENT, TRITURBINE

### Batch 43 — pending (10)
TTML, TVSMOTOR, UBL, UCOBANK, UNIONBANK, UNITDSPR, UNOMINDA, UPL, URBANCO, USHAMART

### Batch 44 — pending (10)
UTIAMC, VBL, VEDL, VIJAYA, VMM, VOLTAS, VTL, WAAREEENER, WELCORP, WELSPUNLIV

### Batch 45 — pending (9)
WHIRLPOOL, WOCKPHARMA, YESBANK, ZEEL, ZENSARTECH, ZENTEC, ZFCVINDIA, ZYDUSLIFE, ZYDUSWELL

## USA Batch (12 companies on file)

Small enough for one batch — the question is which recurring job they're
ready for, not how to chunk them:

### USA Batch 1 — ready for price history, blocked for quarterly financials (12)
AAPL, AMZN, BRKB, GOOGL, LYFT, META, MSFT, NFLX, NVDA, SBUX, UBER, WMT

- **Price history**: mechanically ready (`sources/yfinance_prices.py` is
  ticker-agnostic) once `scripts/fetch_daily_prices.py`'s NSE-500-specific
  universe query is generalized or a US-scoped sibling script is written —
  see "Other Planned Scheduled Jobs" above. No NSE XBRL involved here at
  all (US companies aren't NSE-listed) — this is a yfinance-only job.
- **Quarterly financials**: blocked on the same fiscal-quarter-mapping gap
  already documented above (`sources/yfinance_financials.py` is annual-only
  today). Batching these 12 tickers doesn't change that — the gap is a
  mapping-logic problem per company, not a matter of running more tickers
  through the existing pilot.

