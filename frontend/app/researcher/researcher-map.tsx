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
const MUNICIPALITY_LAYERS = [UNCOVERED_LAYER, COVERED_LAYER, SELECTED_RING_LAYER, SELECTED_LABEL_LAYER];
const SITE_LAYERS = [SITE_CLUSTER_LAYER, SITE_CLUSTER_COUNT_LAYER, SITE_LAYER, SITE_LABEL_LAYER];
const CATCHMENT_LAYERS = [CATCHMENT_FILL, CATCHMENT_LINE];
const MA_BOUNDS: maplibregl.LngLatBoundsLike = [[-73.6, 41.15], [-69.75, 43.05]];
const EARTH_RADIUS_KM = 6371.0088;

type VisualizationMode = "access" | "planning";
type MapStatus = "initializing" | "ready" | "error";
type LayerVisibility = {communities: boolean; sites: boolean; reach: boolean};

function municipalityData(data: GeoCollection, selectedLocations: SelectedLocation[], coveredIds: Set<string>, mode: VisualizationMode): GeoJSON.FeatureCollection<GeoJSON.Point> {
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

function addLocalLabelImages(map: MapLibreMap, maximumClusterCount: number) {
  if (!map.hasImage("site-plus")) map.addImage("site-plus", createLabelImage("+", "#ffffff"), {pixelRatio: 2});
  for (let value = 1; value <= Math.max(20, maximumClusterCount); value += 1) {
    if (!map.hasImage(`cluster-count-${value}`)) map.addImage(`cluster-count-${value}`, createLabelImage(String(value), "#ffffff"), {pixelRatio: 2});
    if (value <= 20 && !map.hasImage(`selection-label-${value}`)) map.addImage(`selection-label-${value}`, createLabelImage(String(value), "#3e2604"), {pixelRatio: 2});
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
    [`Distinct studies within ${accessRadius}`, nearby === null ? "Unavailable" : nearby.toLocaleString()],
    ["Nearest matching mapped study", nearest === null ? (nearby === null ? "Unavailable" : "No resolved matching site") : formatMiles(kilometersToMiles(nearest))],
    ["Current outreach status", mode === "planning" ? selected ? "Selected outreach area" : covered ? "Modeled covered" : "Modeled uncovered" : "Not applicable in access workflow"],
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

export default function ResearcherMap({data, selectedLocations, coveredIds, submittedRadiusKm, visualizationMode, onSelect}: {data: GeoCollection; selectedLocations: SelectedLocation[]; coveredIds: Set<string>; submittedRadiusKm: number | null; visualizationMode: VisualizationMode; onSelect: (feature: GeoFeature) => void}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<MapLibreMap | null>(null);
  const propsRef = useRef({data, selectedLocations, coveredIds, submittedRadiusKm, visualizationMode, onSelect});
  const [mapStatus, setMapStatus] = useState<MapStatus>("initializing");
  const [mapError, setMapError] = useState<string | null>(null);
  const [layers, setLayers] = useState<LayerVisibility>({communities: true, sites: true, reach: true});
  propsRef.current = {data, selectedLocations, coveredIds, submittedRadiusKm, visualizationMode, onSelect};

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
      addLocalLabelImages(map, current.data.recruiting_site_features.length);
      map.addSource(CATCHMENT_SOURCE, {type: "geojson", data: catchmentData(current.selectedLocations, current.submittedRadiusKm)});
      map.addSource(MUNICIPALITY_SOURCE, {type: "geojson", data: municipalityData(current.data, current.selectedLocations, current.coveredIds, current.visualizationMode)});
      map.addSource(SITE_SOURCE, {type: "geojson", data: siteData(current.data), cluster: true, clusterMaxZoom: 13, clusterRadius: 42});
      map.addLayer({id: CATCHMENT_FILL, type: "fill", source: CATCHMENT_SOURCE, paint: {"fill-color": "#d99a27", "fill-opacity": 0.14}});
      map.addLayer({id: CATCHMENT_LINE, type: "line", source: CATCHMENT_SOURCE, paint: {"line-color": "#85540b", "line-width": 1.7, "line-dasharray": [3, 2]}});
      map.addLayer({
        id: UNCOVERED_LAYER,
        type: "circle",
        source: MUNICIPALITY_SOURCE,
        filter: ["all", ["==", ["get", "selected"], 0], ["any", ["==", ["get", "mode_planning"], 0], ["==", ["get", "covered"], 0]]],
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["get", "population"], 0, 5, 10000, 7, 75000, 12],
          "circle-color": [
            "case",
            ["==", ["get", "mode_planning"], 1], "#e6ecea",
            ["==", ["get", "access_available"], 0], "#d7dddb",
            ["==", ["get", "nearby_nct_count"], 0], "#f3f5f4",
            ["interpolate", ["linear"], ["get", "nearby_nct_count"], 1, "#b8c7ef", 3, "#6d83d7", 8, "#323783", 20, "#201a58"],
          ],
          "circle-opacity": 0.94,
          "circle-stroke-width": 1.7,
          "circle-stroke-color": ["case", ["==", ["get", "mode_planning"], 1], "#657a75", ["==", ["get", "nearby_nct_count"], 0], "#7a8985", "#ffffff"],
        },
      });
      map.addLayer({id: COVERED_LAYER, type: "circle", source: MUNICIPALITY_SOURCE, filter: ["all", ["==", ["get", "mode_planning"], 1], ["==", ["get", "covered"], 1], ["==", ["get", "selected"], 0]], paint: {"circle-radius": ["interpolate", ["linear"], ["get", "population"], 0, 5, 10000, 7, 75000, 12], "circle-color": "#16978b", "circle-opacity": 0.9, "circle-stroke-width": 1.5, "circle-stroke-color": "#ffffff"}});
      map.addLayer({id: SITE_CLUSTER_LAYER, type: "circle", source: SITE_SOURCE, filter: ["has", "point_count"], paint: {"circle-color": "#5856a6", "circle-radius": ["step", ["get", "point_count"], 15, 10, 19, 30, 24], "circle-stroke-color": "#ffffff", "circle-stroke-width": 2, "circle-opacity": 0.92}});
      map.addLayer({id: SITE_CLUSTER_COUNT_LAYER, type: "symbol", source: SITE_SOURCE, filter: ["has", "point_count"], layout: {"icon-image": ["concat", "cluster-count-", ["to-string", ["get", "point_count"]]], "icon-size": 1, "icon-allow-overlap": true}});
      map.addLayer({id: SITE_LAYER, type: "circle", source: SITE_SOURCE, filter: ["!", ["has", "point_count"]], paint: {"circle-radius": 8, "circle-color": "#625bc2", "circle-stroke-color": "#ffffff", "circle-stroke-width": 2}});
      map.addLayer({id: SITE_LABEL_LAYER, type: "symbol", source: SITE_SOURCE, filter: ["!", ["has", "point_count"]], layout: {"icon-image": "site-plus", "icon-size": 1, "icon-allow-overlap": true}});
      map.addLayer({id: SELECTED_RING_LAYER, type: "circle", source: MUNICIPALITY_SOURCE, filter: ["==", ["get", "selected"], 1], paint: {"circle-radius": 15, "circle-color": "#f5bd4f", "circle-stroke-color": "#633d06", "circle-stroke-width": 4}});
      map.addLayer({id: SELECTED_LABEL_LAYER, type: "symbol", source: MUNICIPALITY_SOURCE, filter: ["==", ["get", "selected"], 1], layout: {"icon-image": ["concat", "selection-label-", ["to-string", ["get", "selection_order"]]], "icon-size": 1, "icon-allow-overlap": true}});

      const handleMunicipalityClick = (event: maplibregl.MapLayerMouseEvent) => {
        const mapFeature = event.features?.[0];
        const id = String(mapFeature?.id ?? mapFeature?.properties?.municipality ?? "");
        const feature = propsRef.current.data.features.find((item) => item.id === id);
        if (!feature?.geometry) return;
        const selected = propsRef.current.selectedLocations.find((item) => item.municipality_id === id);
        propsRef.current.onSelect(feature);
        new maplibregl.Popup({offset: 14, closeButton: true, closeOnMove: true}).setLngLat(feature.geometry.coordinates).setDOMContent(municipalityPopup(feature, selected, propsRef.current.coveredIds.has(id), propsRef.current.submittedRadiusKm, propsRef.current.visualizationMode)).addTo(map);
      };
      MUNICIPALITY_LAYERS.forEach((layer) => {
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
    (map.getSource(MUNICIPALITY_SOURCE) as GeoJSONSource | undefined)?.setData(municipalityData(data, selectedLocations, coveredIds, visualizationMode));
    (map.getSource(SITE_SOURCE) as GeoJSONSource | undefined)?.setData(siteData(data));
    (map.getSource(CATCHMENT_SOURCE) as GeoJSONSource | undefined)?.setData(catchmentData(selectedLocations, submittedRadiusKm));
  }, [coveredIds, data, selectedLocations, submittedRadiusKm, visualizationMode]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map?.isStyleLoaded()) return;
    setVisibility(map, MUNICIPALITY_LAYERS, layers.communities);
    setVisibility(map, SITE_LAYERS, layers.sites);
    setVisibility(map, CATCHMENT_LAYERS, layers.reach);
  }, [layers, mapStatus]);

  const toggle = (key: keyof LayerVisibility) => setLayers((current) => ({...current, [key]: !current[key]}));
  return <div className={styles.mapFrame}>
    <div ref={containerRef} className={styles.map} role="region" tabIndex={0} aria-label="Interactive Massachusetts map. Use map controls to zoom; use the layer controls and select markers for details." data-map-status={mapStatus} data-municipality-count={resolvedMunicipalityCount} data-site-count={resolvedSiteCount} data-selected-count={selectedLocations.length} data-covered-count={coveredIds.size} />
    <fieldset className={styles.layerControls}><legend>Show map layers</legend><label><input type="checkbox" checked={layers.communities} onChange={() => toggle("communities")} /> Communities</label><label><input type="checkbox" checked={layers.sites} onChange={() => toggle("sites")} /> Study sites</label>{visualizationMode === "planning" && <label><input type="checkbox" checked={layers.reach} onChange={() => toggle("reach")} /> Event reach</label>}</fieldset>
    <div className={`${styles.mapStatus} ${mapStatus === "error" ? styles.mapStatusError : ""}`} role={mapStatus === "error" ? "alert" : "status"}>{mapStatus === "initializing" ? "Initializing map…" : mapStatus === "ready" ? "Interactive map loaded" : `Map error: ${mapError ?? "Unknown rendering failure"}`}</div>
  </div>;
}
