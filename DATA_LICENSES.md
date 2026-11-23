# Data sources and licences

| Source | Use | Licence / terms | Redistributed in this repo? |
|---|---|---|---|
| aisstream.io real-time AIS | Eastern Med case study (collected by us) | Terms page returned HTTP 404 on 2026-11-13; documentation states no SLA and no replay. **Open: confirm storage/redistribution terms before any publication.** | No - raw data stays in `private/` |
| Danish Maritime Authority historical AIS | Development / evaluation region | DMA conditions for AIS data use (to be quoted in Phase 1) | No |
| EMODnet Human Activities (telecom cables, power cables, pipelines) | Infrastructure layer | CC BY 4.0 (to be confirmed per dataset in Phase 1) | Possibly derived buffers, with attribution |
| EMODnet Human Activities wind farms (polygons) | Context: operating wind farms | CC BY 4.0 | No |
| World Port Index (via previous project) | Context: port areas | Public domain (US NGA) | No |
| OpenStreetMap (Overpass API) submarine cables and pipelines | Infrastructure layer (community accuracy) | ODbL 1.0, attribution "(c) OpenStreetMap contributors"; derived databases must stay ODbL | No raw data; figures carry attribution |
| Natural Earth 10 m land | Map background (report figures) | Public domain | No |
| CARTO basemap tiles (OpenStreetMap data) | Dashboard background map | (c) OpenStreetMap contributors, (c) CARTO - attribution shown on the map | No |
| TeleGeography Submarine Cable Map | Optional visual reference only | CC BY-NC-SA 3.0 (non-commercial) | No |
| EMODnet Bathymetry DTM (WCS `emodnet__mean`) | Water depth at each visit | CC BY 4.0 | No |
| GEBCO gridded bathymetry | Not used (EMODnet Bathymetry covers both regions) | GEBCO terms | No |

Every entry is re-checked when the source is first used in its phase.
