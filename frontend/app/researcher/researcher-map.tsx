"use client";

import {useEffect, useMemo, useRef, useState} from "react";
import * as maplibregl from "maplibre-gl";
import type {GeoJSONSource, Map as MapLibreMap} from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";

import {formatMiles, kilometersToMiles} from "./researcher-utils";
import type {GeoCollection, GeoFeature, RecruitingSiteFeature, SelectedLocation} from "./types";
import styles from "./researcher.module.css";

const MUNICIPALITY_SOURCE = "municipalities";
const SITE_SOURCE = "recruiting-sites";
const CATCHMENT_SOURCE = "outreach-catchments";
const CATCHMENT_FILL = "catchment-fill";
const CATCHMENT_LINE = "catchment-line";
const UNCOVERED_LAYER = "uncovered-communities";
const COVERED_LAYER = "covered-communities";
const SITE_CLUSTER_LAYER = "site-clusters";
const SITE_CLUSTER_COUNT_LAYER = "site-cluster-count";
const SITE_LAYER = "recruiting-site-symbols";
const SITE_LABEL_LAYER = "recruiting-site-label";
const SELECTED_RING_LAYER = "selected-outreach-rings";
const SELECTED_LABEL_LAYER = "selected-outreach-labels";
const ACTIVE_COMMUNITY_LAYER = "active-community-ring";
const MUNICIPALITY_LAYERS = [UNCOVERED_LAYER, COVERED_LAYER, SELECTED_RING_LAYER, SELECTED_LABEL_LAYER, ACTIVE_COMMUNITY_LAYER];
const MUNICIPALITY_CLICK_LAYERS = [UNCOVERED_LAYER, COVERED_LAYER, SELECTED_RING_LAYER, SELECTED_LABEL_LAYER];
const SITE_LAYERS = [SITE_CLUSTER_LAYER, SITE_CLUSTER_COUNT_LAYER, SITE_LAYER, SITE_LABEL_LAYER];
const CATCHMENT_LAYERS = [CATCHMENT_FILL, CATCHMENT_LINE];
const MA_BOUNDS: maplibregl.LngLatBoundsLike = [[-73.6, 41.15], [-69.75, 43.05]];
const EARTH_RADIUS_KM = 6371.0088;

type VisualizationMode = "access" | "planning";
type MapStatus = "initializing" | "ready" | "error";
type LayerVisibility = {communities: boolean; sites: boolean; reach: boolean};

function municipalityData(data: GeoCollection, selectedLocations: SelectedLocation[], coveredIds: Set<string>, mode: VisualizationMode, activeFeatureId: string | null): GeoJSON.FeatureCollection<GeoJSON.Point> {
  const order = new Map(selectedLocations.map((location, index) => [location.municipality_id, index + 1]));
  return {type: "FeatureCollection", features: data.features.flatMap((feature) => feature.geometry ? [{
    type: "Feature" as const,
    id: feature.id,
    geometry: feature.geometry,
    properties: {
      ...feature.properties,
      selected: order.has(feature.id) ? 1 : 0,
      selection_order: order.get(feature.id) ?? 0,
      covered: coveredIds.has(feature.id) ? 1 : 0,
      mode_planning: mode === "planning" ? 1 : 0,
      active: feature.id === activeFeatureId ? 1 : 0,
      population: feature.properties.ej_subset_population ?? 0,
      access_available: feature.properties.distinct_nearby_recruiting_nct_count === null ? 0 : 1,
      nearby_nct_count: feature.properties.distinct_nearby_recruiting_nct_count ?? -1,
    },
  }] : [])};
}

function siteData(data: GeoCollection): GeoJSON.FeatureCollection<GeoJSON.Point> {
  return {type: "FeatureCollection", features: data.recruiting_site_features.flatMap((feature) => feature.geometry ? [{type: "Feature" as const, id: feature.id, geometry: feature.geometry, properties: {id: feature.id, nct_id: feature.properties.nct_id, facility: feature.properties.facility}}] : [])};
}

function destinationPoint(longitude: number, latitude: number, distanceKm: number, bearingRadians: number): [number, number] {
  const angularDistance = distanceKm / EARTH_RADIUS_KM;
  const latitudeRadians = latitude * Math.PI / 180;
  const longitudeRadians = longitude * Math.PI / 180;
  const destinationLatitude = Math.asin(Math.sin(latitudeRadians) * Math.cos(angularDistance) + Math.cos(latitudeRadians) * Math.sin(angularDistance) * Math.cos(bearingRadians));
  const destinationLongitude = longitudeRadians + Math.atan2(Math.sin(bearingRadians) * Math.sin(angularDistance) * Math.cos(latitudeRadians), Math.cos(angularDistance) - Math.sin(latitudeRadians) * Math.sin(destinationLatitude));
  return [((destinationLongitude * 180 / Math.PI + 540) % 360) - 180, destinationLatitude * 180 / Math.PI];
}

export function catchmentData(selectedLocations: SelectedLocation[], radiusKm: number | null): GeoJSON.FeatureCollection<GeoJSON.Polygon> {
  if (radiusKm === null || radiusKm <= 0) return {type: "FeatureCollection", features: []};
  return {type: "FeatureCollection", features: selectedLocations.map((location, index) => ({
    type: "Feature" as const,
    id: `catchment-${location.municipality_id}`,
    geometry: {type: "Polygon" as const, coordinates: [Array.from({length: 65}, (_, pointIndex) => destinationPoint(location.longitude, location.latitude, radiusKm, (Math.PI * 2 * (pointIndex % 64)) / 64))]},
    properties: {municipality: location.municipality_id, selection_order: index + 1, radius_km: radiusKm},
  }))};
}

function createLabelImage(label: string, color: string): ImageData {
  const canvas = document.createElement("canvas");
  canvas.width = 48;
  canvas.height = 48;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("Canvas labels are unavailable in this browser.");
  context.fillStyle = color;
  context.font = "800 22px system-ui, sans-serif";
  context.textAlign = "center";
  context.textBaseline = "middle";
  context.fillText(label, 24, 25, 44);
  return context.getImageData(0, 0, 48, 48);
}

function addLocalLabelImages(map: MapLibreMap) {
  for (let value = 1; value <= 20; value += 1) {
    if (!map.hasImage(`selection-label-${value}`)) map.addImage(`selection-label-${value}`, createLabelImage(String(value), "#451a03"), {pixelRatio: 2});
  }
}

function popupBody(title: string, rows: Array<[string, string]>, warning?: string, officialStudyUrl?: string | null) {
  const root = document.createElement("div");
  root.className = styles.popupBody;
  const heading = document.createElement("strong");
  heading.textContent = title;
  root.appendChild(heading);
  for (const [label, value] of rows) {
    const row = document.createElement("span");
    const labelNode = document.createElement("b");
    labelNode.textContent = `${label}: `;
    row.append(labelNode, document.createTextNode(value));
    root.appendChild(row);
  }
  if (officialStudyUrl) {
    const link = document.createElement("a");
    link.href = officialStudyUrl;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = "Open official ClinicalTrials.gov study";
    root.appendChild(link);
  }
  if (warning) {
    const note = document.createElement("small");
    note.textContent = warning;
    root.appendChild(note);
  }
  return root;
}

function officialStudyUrl(nctId: string) {
  const normalized = nctId.trim().toUpperCase();
  return /^NCT\d{8}$/.test(normalized) ? `https://clinicaltrials.gov/study/${normalized}` : null;
}

function municipalityPopup(feature: GeoFeature, selected: SelectedLocation | undefined, covered: boolean, submittedRadiusKm: number | null, mode: VisualizationMode) {
  const properties = feature.properties;
  const nearby = properties.distinct_nearby_recruiting_nct_count;
  const nearest = properties.nearest_resolved_recruiting_site_distance_km;
  const accessRadius = properties.access_radius_km === null ? "the chosen distance" : formatMiles(kilometersToMiles(properties.access_radius_km));
  const rows: Array<[string, string]> = [
    ["Represented EJ-area population", properties.ej_subset_population === null ? "Unavailable" : properties.ej_subset_population.toLocaleString()],
    ["Documented minority share", properties.population_weighted_percent_minority_ej_subset === null ? "Unavailable" : `${properties.population_weighted_percent_minority_ej_subset.toFixed(1)}%`],
    ["Limited-English households", properties.population_weighted_limited_english_household_percent_ej_subset === null ? "Unavailable" : `${properties.population_weighted_limited_english_household_percent_ej_subset.toFixed(1)}%`],
    [`Clinical trials within ${accessRadius}`, nearby === null ? "Unavailable" : nearby.toLocaleString()],
    ["Nearest matching recruiting location", nearest === null ? (nearby === null ? "Unavailable" : "No resolved matching location") : `${formatMiles(kilometersToMiles(nearest))} straight-line`],
    ["Outreach context", mode === "planning" ? selected ? "Recommended outreach area" : covered ? "Within modeled event reach" : "Outside modeled event reach" : "Use the access result above"],
  ];
  if (selected) {
    rows.push(["Communities in event reach", selected.explanation.raw_catchment_community_count.toLocaleString()]);
    rows.push(["Event reach", submittedRadiusKm === null ? "Unavailable" : formatMiles(kilometersToMiles(submittedRadiusKm))]);
  }
  return popupBody(feature.id, rows, properties.access_distance_limitation ?? "Representative analytical point—not a boundary, venue, or patient location.");
}

function sitePopup(feature: RecruitingSiteFeature) {
  const properties = feature.properties;
  const locality = [properties.city, properties.state, properties.postal_code].filter(Boolean).join(", ");
  const coordinateDescription = properties.coordinate_resolution_status === "verified_geocode" ? "Verified geocode" : properties.coordinate_resolution_status === "source_coordinates" ? "Source-supplied; may be approximate or city-level" : "Unresolved";
  return popupBody(properties.facility || "Facility unavailable", [
    ["NCT ID", properties.nct_id],
    ["Location", locality || "Unavailable"],
    ["Recruiting status", properties.recruiting_status || "Unavailable"],
    ["Condition", properties.conditions.length ? properties.conditions.join("; ") : "Unavailable"],
    ["Coordinate precision", coordinateDescription],
    ["Coordinate source", properties.coordinate_source || "Unavailable"],
    ["Retrieved", properties.retrieved_at ? new Date(properties.retrieved_at).toLocaleDateString("en-US") : "Unavailable"],
  ], properties.coordinate_approximation_warning, officialStudyUrl(properties.nct_id));
}

function setVisibility(map: MapLibreMap, layers: string[], visible: boolean) {
  for (const layer of layers) if (map.getLayer(layer)) map.setLayoutProperty(layer, "visibility", visible ? "visible" : "none");
}

export default function ResearcherMap({data, selectedLocations, coveredIds, submittedRadiusKm, visualizationMode, activeFeatureId, onSelect}: {data: GeoCollection; selectedLocations: SelectedLocation[]; coveredIds: Set<string>; submittedRadiusKm: number | null; visualizationMode: VisualizationMode; activeFeatureId: string | null; onSelect: (feature: GeoFeature) => void}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const propsRef = useRef({data, selectedLocations, coveredIds, submittedRadiusKm, visualizationMode, activeFeatureId, onSelect});
  const [mapStatus, setMapStatus] = useState<MapStatus>("initializing");
  const [mapError, setMapError] = useState<string | null>(null);
  const [layers, setLayers] = useState<LayerVisibility>({communities: true, sites: false, reach: true});
  propsRef.current = {data, selectedLocations, coveredIds, submittedRadiusKm, visualizationMode, activeFeatureId, onSelect};

  const resolvedMunicipalityCount = useMemo(() => data.features.filter((feature) => feature.geometry).length, [data]);
  const resolvedSiteCount = useMemo(() => data.recruiting_site_features.filter((feature) => feature.geometry).length, [data]);

  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;
    let map: MapLibreMap;
    try {
      maplibregl.setWorkerUrl(new URL("/maplibre-gl-worker.mjs", window.location.origin).toString());
      map = new maplibregl.Map({container: containerRef.current, bounds: MA_BOUNDS, fitBoundsOptions: {padding: 24}, attributionControl: false, style: {version: 8, sources: {osm: {type: "raster", tiles: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"], tileSize: 256, attribution: "© OpenStreetMap contributors"}}, layers: [{id: "osm", type: "raster", source: "osm", paint: {"raster-saturation": -0.75, "raster-opacity": 0.86}}]}});
    } catch (error) {
      setMapStatus("error");
      setMapError(error instanceof Error ? error.message : "MapLibre could not initialize WebGL.");
      return;
    }
    mapRef.current = map;
    map.addControl(new maplibregl.NavigationControl({showCompass: false}), "top-right");
    map.addControl(new maplibregl.AttributionControl({compact: true}), "bottom-right");
    map.on("error", (event) => {
      if (mapRef.current !== map) return;
      const message = event.error?.message || "MapLibre reported an unknown rendering error.";
      if (/worker|webgl|gpu|context|style/i.test(message)) { setMapStatus("error"); setMapError(message); }
      else console.warn("Nonfatal map resource error:", message);
    });
    map.on("load", () => {
      if (mapRef.current !== map) return;
      const current = propsRef.current;
      addLocalLabelImages(map);
      map.addSource(CATCHMENT_SOURCE, {type: "geojson", data: catchmentData(current.selectedLocations, current.submittedRadiusKm)});
      map.addSource(MUNICIPALITY_SOURCE, {type: "geojson", data: municipalityData(current.data, current.selectedLocations, current.coveredIds, current.visualizationMode, current.activeFeatureId)});
      map.addSource(SITE_SOURCE, {type: "geojson", data: siteData(current.data), cluster: true, clusterMaxZoom: 13, clusterRadius: 34});
      map.addLayer({id: CATCHMENT_FILL, type: "fill", source: CATCHMENT_SOURCE, paint: {"fill-color": "#0b766e", "fill-opacity": 0.14}});
      map.addLayer({id: CATCHMENT_LINE, type: "line", source: CATCHMENT_SOURCE, paint: {"line-color": "#0b766e", "line-width": 1.4, "line-dasharray": [3, 2]}});
      map.addLayer({
        id: UNCOVERED_LAYER,
        type: "circle",
        source: MUNICIPALITY_SOURCE,
        filter: ["all", ["==", ["get", "selected"], 0], ["any", ["==", ["get", "mode_planning"], 0], ["==", ["get", "covered"], 0]]],
        paint: {
          "circle-radius": ["case", ["==", ["get", "mode_planning"], 1], 4.5, 6],
          "circle-color": ["case", ["==", ["get", "mode_planning"], 1], "#cbd5e1", ["==", ["get", "access_available"], 0], "#94a3b8", ["==", ["get", "nearby_nct_count"], 0], "#f59e0b", "#0b766e"],
          "circle-opacity": 0.9,
          "circle-stroke-width": 1.2,
          "circle-stroke-color": "#ffffff",
        },
      });
      map.addLayer({id: COVERED_LAYER, type: "circle", source: MUNICIPALITY_SOURCE, filter: ["all", ["==", ["get", "mode_planning"], 1], ["==", ["get", "covered"], 1], ["==", ["get", "selected"], 0]], paint: {"circle-radius": 5.5, "circle-color": "#34d399", "circle-opacity": 0.9, "circle-stroke-width": 1.2, "circle-stroke-color": "#ffffff"}});
      map.addLayer({id: SITE_CLUSTER_LAYER, type: "circle", source: SITE_SOURCE, filter: ["has", "point_count"], paint: {"circle-color": "#11211f", "circle-radius": ["step", ["get", "point_count"], 10, 10, 12, 30, 14], "circle-stroke-color": "#ffffff", "circle-stroke-width": 1.5, "circle-opacity": 0.88}});
      map.addLayer({id: SITE_CLUSTER_COUNT_LAYER, type: "symbol", source: SITE_SOURCE, filter: ["has", "point_count"], layout: {"text-field": ["get", "point_count_abbreviated"], "text-size": 9, "text-allow-overlap": true}, paint: {"text-color": "#ffffff"}});
      map.addLayer({id: SITE_LAYER, type: "circle", source: SITE_SOURCE, filter: ["!", ["has", "point_count"]], paint: {"circle-radius": 4.5, "circle-color": "#11211f", "circle-stroke-color": "#ffffff", "circle-stroke-width": 1.2}});
      map.addLayer({id: SITE_LABEL_LAYER, type: "symbol", source: SITE_SOURCE, filter: ["!", ["has", "point_count"]], layout: {"text-field": "+", "text-size": 8, "text-allow-overlap": true}, paint: {"text-color": "#ffffff"}});
      map.addLayer({id: SELECTED_RING_LAYER, type: "circle", source: MUNICIPALITY_SOURCE, filter: ["==", ["get", "selected"], 1], paint: {"circle-radius": 14, "circle-color": "#f59e0b", "circle-stroke-color": "#78350f", "circle-stroke-width": 3}});
      map.addLayer({id: SELECTED_LABEL_LAYER, type: "symbol", source: MUNICIPALITY_SOURCE, filter: ["==", ["get", "selected"], 1], layout: {"icon-image": ["concat", "selection-label-", ["to-string", ["get", "selection_order"]]], "icon-size": 1, "icon-allow-overlap": true}});
      map.addLayer({id: ACTIVE_COMMUNITY_LAYER, type: "circle", source: MUNICIPALITY_SOURCE, filter: ["==", ["get", "active"], 1], paint: {"circle-radius": 18, "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": "#11211f", "circle-stroke-width": 3}});

      const handleMunicipalityClick = (event: maplibregl.MapLayerMouseEvent) => {
        const mapFeature = event.features?.[0];
        const id = String(mapFeature?.id ?? mapFeature?.properties?.municipality ?? "");
        const feature = propsRef.current.data.features.find((item) => item.id === id);
        if (!feature?.geometry) return;
        const selected = propsRef.current.selectedLocations.find((item) => item.municipality_id === id);
        propsRef.current.onSelect(feature);
        new maplibregl.Popup({offset: 14, closeButton: true, closeOnMove: true}).setLngLat(feature.geometry.coordinates).setDOMContent(municipalityPopup(feature, selected, propsRef.current.coveredIds.has(id), propsRef.current.submittedRadiusKm, propsRef.current.visualizationMode)).addTo(map);
      };
      MUNICIPALITY_CLICK_LAYERS.forEach((layer) => {
        map.on("click", layer, handleMunicipalityClick);
        map.on("mouseenter", layer, () => { map.getCanvas().style.cursor = "pointer"; });
        map.on("mouseleave", layer, () => { map.getCanvas().style.cursor = ""; });
      });
      const handleSiteClick = (event: maplibregl.MapLayerMouseEvent) => {
        const mapFeature = event.features?.[0];
        const id = String(mapFeature?.id ?? mapFeature?.properties?.id ?? "");
        const feature = propsRef.current.data.recruiting_site_features.find((item) => item.id === id);
        if (!feature?.geometry) return;
        new maplibregl.Popup({offset: 12, closeButton: true, closeOnMove: true}).setLngLat(feature.geometry.coordinates).setDOMContent(sitePopup(feature)).addTo(map);
      };
      [SITE_LAYER, SITE_LABEL_LAYER].forEach((layer) => {
        map.on("click", layer, handleSiteClick);
        map.on("mouseenter", layer, () => { map.getCanvas().style.cursor = "pointer"; });
        map.on("mouseleave", layer, () => { map.getCanvas().style.cursor = ""; });
      });
      map.on("click", SITE_CLUSTER_LAYER, (event) => {
        const cluster = map.queryRenderedFeatures(event.point, {layers: [SITE_CLUSTER_LAYER]})[0];
        const clusterId = Number(cluster?.properties?.cluster_id);
        if (!Number.isFinite(clusterId) || cluster?.geometry.type !== "Point") return;
        const clusterCoordinates = cluster.geometry.coordinates as [number, number];
        const source = map.getSource(SITE_SOURCE) as GeoJSONSource;
        void source.getClusterExpansionZoom(clusterId).then((zoom) => map.easeTo({center: clusterCoordinates, zoom})).catch((error: unknown) => {setMapStatus("error"); setMapError(error instanceof Error ? error.message : "Unable to expand this site cluster.");});
      });
      map.on("mouseenter", SITE_CLUSTER_LAYER, () => { map.getCanvas().style.cursor = "zoom-in"; });
      map.on("mouseleave", SITE_CLUSTER_LAYER, () => { map.getCanvas().style.cursor = ""; });
      setMapStatus("ready");
      setMapError(null);
    });
    return () => { map.remove(); mapRef.current = null; };
  }, []);

  useEffect(() => {
    const map = mapRef.current;
    if (!map?.isStyleLoaded()) return;
    (map.getSource(MUNICIPALITY_SOURCE) as GeoJSONSource | undefined)?.setData(municipalityData(data, selectedLocations, coveredIds, visualizationMode, activeFeatureId));
    (map.getSource(SITE_SOURCE) as GeoJSONSource | undefined)?.setData(siteData(data));
    (map.getSource(CATCHMENT_SOURCE) as GeoJSONSource | undefined)?.setData(catchmentData(selectedLocations, submittedRadiusKm));
  }, [activeFeatureId, coveredIds, data, selectedLocations, submittedRadiusKm, visualizationMode]);

  useEffect(() => {
    if (!activeFeatureId) return;
    const map = mapRef.current;
    const feature = data.features.find((item) => item.id === activeFeatureId);
    if (!map?.isStyleLoaded() || !feature?.geometry) return;
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    map.easeTo({center: feature.geometry.coordinates, zoom: Math.max(map.getZoom(), 8), duration: reducedMotion ? 0 : 500});
  }, [activeFeatureId, data, mapStatus]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map?.isStyleLoaded()) return;
    setVisibility(map, MUNICIPALITY_LAYERS, layers.communities);
    setVisibility(map, SITE_LAYERS, layers.sites);
    setVisibility(map, CATCHMENT_LAYERS, layers.reach);
  }, [layers, mapStatus]);

  const toggle = (key: keyof LayerVisibility) => setLayers((current) => ({...current, [key]: !current[key]}));
  return <div className={styles.mapFrame}>
    <div ref={containerRef} className={styles.map} role="region" tabIndex={0} aria-label="Interactive Massachusetts map. Use map controls to zoom and pan, toggle layers, and select community markers for details." data-map-status={mapStatus} data-municipality-count={resolvedMunicipalityCount} data-site-count={resolvedSiteCount} data-selected-count={selectedLocations.length} data-covered-count={coveredIds.size} />
    <fieldset className={styles.layerControls}><legend>Map layers</legend><label><input type="checkbox" checked={layers.communities} onChange={() => toggle("communities")} /> Communities</label><label><input type="checkbox" checked={layers.sites} onChange={() => toggle("sites")} /> Recruiting locations</label>{visualizationMode === "planning" && <label><input type="checkbox" checked={layers.reach} onChange={() => toggle("reach")} /> Modeled event reach</label>}</fieldset>
    <div className={styles.legend} aria-label={`${visualizationMode === "access" ? "Trial access" : "Outreach"} map legend`}><div className={styles.legendTitle}>{visualizationMode === "access" ? "Trial access" : "Outreach plan"}</div>{visualizationMode === "access" ? <><div className={styles.legendRow}><i className={styles.dot} /> Nearby trial found</div><div className={styles.legendRow}><i className={`${styles.dot} ${styles.dotGap}`} /> No nearby trial found</div><div className={styles.legendRow}><i className={`${styles.dot} ${styles.dotUnavailable}`} /> Access unavailable</div>{layers.sites && <div className={styles.legendRow}><i className={`${styles.dot} ${styles.dotSite}`} /> Recruiting location</div>}</> : <><div className={styles.legendRow}><i className={`${styles.dot} ${styles.dotSelected}`} /> Recommended outreach area</div>{layers.reach && <div className={styles.legendRow}><i className={styles.legendCatchment} /> Within modeled event reach</div>}<div className={styles.legendRow}><i className={`${styles.dot} ${styles.dotOutside}`} /> Outside modeled event reach</div></>}</div>
    <div className={`${styles.mapStatus} ${mapStatus === "error" ? styles.mapStatusError : ""}`} role={mapStatus === "error" ? "alert" : "status"}>{mapStatus === "initializing" ? "Initializing map…" : mapStatus === "ready" ? "Interactive map ready" : `Map error: ${mapError ?? "Unknown rendering failure"}`}</div>
  </div>;
}
