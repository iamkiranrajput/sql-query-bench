import {
  Component,
  Input,
  OnChanges,
  OnDestroy,
  AfterViewInit,
  ViewChild,
  ElementRef,
  ChangeDetectionStrategy,
  ChangeDetectorRef,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatIconModule } from '@angular/material/icon';

/**
 * Lightweight, self-contained map view for spatial query results.
 *
 * Renders rows that contain geometry — either a GeoJSON column produced by
 * PostGIS `ST_AsGeoJSON(...)`, or a latitude/longitude column pair — on an
 * interactive OpenStreetMap. This is the visual payoff for the PostGIS spatial
 * demo (e.g. "stores within 5 km of downtown").
 *
 * Design notes:
 * - **No build-time dependency.** Leaflet is loaded on demand from a CDN with
 *   Subresource Integrity (SRI) hashes, so there is no npm install, no bundler
 *   configuration, and no change to the production build. If the CDN is
 *   unreachable (offline / CSP), the component degrades gracefully to a
 *   coordinate list instead of breaking.
 * - Markers use `L.circleMarker`, which needs no image assets (avoids the
 *   classic Leaflet marker-icon 404 with bundlers).
 */

// Leaflet 1.9.4 — official SRI hashes published on leafletjs.com.
const LEAFLET_VERSION = '1.9.4';
const LEAFLET_CSS_URL = `https://unpkg.com/leaflet@${LEAFLET_VERSION}/dist/leaflet.css`;
const LEAFLET_JS_URL = `https://unpkg.com/leaflet@${LEAFLET_VERSION}/dist/leaflet.js`;
const LEAFLET_CSS_SRI = 'sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=';
const LEAFLET_JS_SRI = 'sha256-20nQCchB9co0qIjJZRl6OOAlfwHT7yKb8eDZM+5BIso=';

// Process-wide single load (shared across component instances).
let _leafletPromise: Promise<any> | null = null;

function loadLeaflet(): Promise<any> {
  if (_leafletPromise) return _leafletPromise;

  _leafletPromise = new Promise<any>((resolve, reject) => {
    const w = window as any;
    if (w.L) {
      resolve(w.L);
      return;
    }

    // Stylesheet (idempotent).
    if (!document.querySelector(`link[data-leaflet="${LEAFLET_VERSION}"]`)) {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = LEAFLET_CSS_URL;
      link.integrity = LEAFLET_CSS_SRI;
      link.crossOrigin = 'anonymous';
      link.setAttribute('data-leaflet', LEAFLET_VERSION);
      document.head.appendChild(link);
    }

    const script = document.createElement('script');
    script.src = LEAFLET_JS_URL;
    script.integrity = LEAFLET_JS_SRI;
    script.crossOrigin = 'anonymous';
    script.async = true;
    script.onload = () => {
      if (w.L) resolve(w.L);
      else reject(new Error('Leaflet loaded but window.L is undefined'));
    };
    script.onerror = () => reject(new Error('Failed to load Leaflet from CDN'));
    document.head.appendChild(script);
  });

  return _leafletPromise;
}

interface GeoPoint {
  lat: number;
  lon: number;
  props: Record<string, any>;
}

@Component({
  selector: 'app-map-view',
  standalone: true,
  imports: [CommonModule, MatIconModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="map-view">
      <div class="map-view-header">
        <mat-icon>map</mat-icon>
        <span>Map View</span>
        <span class="map-count" *ngIf="pointCount > 0">{{ pointCount }} location{{ pointCount !== 1 ? 's' : '' }}</span>
      </div>

      <div #mapContainer class="map-canvas" [style.display]="loadError ? 'none' : 'block'"></div>

      <!-- Graceful fallback when the map library can't load (offline / CSP). -->
      <div *ngIf="loadError" class="map-fallback">
        <mat-icon>location_off</mat-icon>
        <p>Interactive map unavailable ({{ loadError }}). Showing coordinates instead.</p>
        <table class="map-fallback-table">
          <thead><tr><th>#</th><th>Latitude</th><th>Longitude</th></tr></thead>
          <tbody>
            <tr *ngFor="let p of points; let i = index">
              <td>{{ i + 1 }}</td>
              <td>{{ p.lat | number: '1.4-6' }}</td>
              <td>{{ p.lon | number: '1.4-6' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  `,
  styles: [
    `
      .map-view {
        border: 1px solid var(--border-color, #e5e7eb);
        border-radius: 10px;
        overflow: hidden;
        background: var(--bg-elevated, #fff);
      }
      .map-view-header {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 10px 14px;
        font-weight: 600;
        font-size: 14px;
        color: var(--text-primary, #111827);
        border-bottom: 1px solid var(--border-subtle, #f3f4f6);
      }
      .map-view-header mat-icon {
        color: #0078d4;
      }
      .map-count {
        margin-left: auto;
        font-size: 12px;
        font-weight: 500;
        color: var(--text-secondary, #6b7280);
      }
      .map-canvas {
        width: 100%;
        height: 380px;
      }
      .map-fallback {
        padding: 16px;
        text-align: center;
        color: var(--text-secondary, #6b7280);
      }
      .map-fallback mat-icon {
        font-size: 32px;
        width: 32px;
        height: 32px;
        opacity: 0.6;
      }
      .map-fallback-table {
        margin: 10px auto 0;
        border-collapse: collapse;
        font-size: 12px;
      }
      .map-fallback-table th,
      .map-fallback-table td {
        border: 1px solid var(--border-subtle, #f3f4f6);
        padding: 3px 10px;
      }
    `,
  ],
})
export class MapViewComponent implements OnChanges, AfterViewInit, OnDestroy {
  /** Result rows to plot. Geometry is auto-detected. */
  @Input() rows: Record<string, any>[] = [];

  @ViewChild('mapContainer') mapContainer?: ElementRef<HTMLDivElement>;

  points: GeoPoint[] = [];
  pointCount = 0;
  loadError = '';

  private map: any = null;
  private layer: any = null;
  private viewReady = false;

  constructor(private cdr: ChangeDetectorRef) {}

  /** True if the given rows contain plottable geometry. */
  static hasGeometry(rows: Record<string, any>[] | null | undefined): boolean {
    if (!rows || !rows.length) return false;
    return MapViewComponent.extractPoints(rows).length > 0;
  }

  ngAfterViewInit(): void {
    this.viewReady = true;
    this.render();
  }

  ngOnChanges(): void {
    this.points = MapViewComponent.extractPoints(this.rows);
    this.pointCount = this.points.length;
    if (this.viewReady) this.render();
  }

  ngOnDestroy(): void {
    if (this.map) {
      try {
        this.map.remove();
      } catch {
        /* ignore */
      }
      this.map = null;
    }
  }

  private render(): void {
    this.points = MapViewComponent.extractPoints(this.rows);
    this.pointCount = this.points.length;
    if (!this.points.length || !this.mapContainer) return;

    loadLeaflet()
      .then((L) => {
        this.loadError = '';
        this.drawMap(L);
        this.cdr.markForCheck();
      })
      .catch((err) => {
        this.loadError = err?.message || 'load failed';
        this.cdr.markForCheck();
      });
  }

  private drawMap(L: any): void {
    const el = this.mapContainer!.nativeElement;

    if (!this.map) {
      this.map = L.map(el, { scrollWheelZoom: true });
      L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19,
        attribution: '&copy; OpenStreetMap contributors',
      }).addTo(this.map);
    }

    if (this.layer) {
      this.map.removeLayer(this.layer);
      this.layer = null;
    }

    const markers = this.points.map((p) => {
      const marker = L.circleMarker([p.lat, p.lon], {
        radius: 8,
        color: '#0078d4',
        fillColor: '#0078d4',
        fillOpacity: 0.7,
        weight: 2,
      });
      marker.bindPopup(this.popupHtml(p.props));
      return marker;
    });

    this.layer = L.featureGroup(markers).addTo(this.map);

    try {
      this.map.fitBounds(this.layer.getBounds().pad(0.2));
    } catch {
      this.map.setView([this.points[0].lat, this.points[0].lon], 11);
    }

    // The container is often created while hidden; recalc size next tick.
    setTimeout(() => this.map && this.map.invalidateSize(), 0);
  }

  /** Build a small popup table from a row's non-geometry properties. */
  private popupHtml(props: Record<string, any>): string {
    const esc = (v: any) =>
      String(v ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
    const rows = Object.entries(props)
      .filter(([, v]) => v !== null && v !== undefined && typeof v !== 'object')
      .slice(0, 8)
      .map(([k, v]) => `<tr><td style="font-weight:600;padding-right:8px">${esc(k)}</td><td>${esc(v)}</td></tr>`)
      .join('');
    return `<table style="font-size:12px">${rows}</table>`;
  }

  // ------------------------------------------------------------------
  // Geometry extraction (static so the parent can gate rendering).
  // ------------------------------------------------------------------
  private static extractPoints(rows: Record<string, any>[]): GeoPoint[] {
    if (!rows || !rows.length) return [];
    const sample = rows[0];
    const cols = Object.keys(sample);

    // 1) GeoJSON column (e.g. from ST_AsGeoJSON).
    const geoCol = cols.find((c) => MapViewComponent.parseGeoJSON(sample[c]) !== null);
    if (geoCol) {
      const pts: GeoPoint[] = [];
      for (const row of rows) {
        const centroid = MapViewComponent.geoJsonCentroid(MapViewComponent.parseGeoJSON(row[geoCol]));
        if (centroid) {
          const props = { ...row };
          delete props[geoCol];
          pts.push({ lat: centroid.lat, lon: centroid.lon, props });
        }
      }
      if (pts.length) return pts;
    }

    // 2) Latitude / longitude column pair.
    const latCol = cols.find((c) => /^(lat|latitude|y)$/i.test(c));
    const lonCol = cols.find((c) => /^(lon|lng|long|longitude|x)$/i.test(c));
    if (latCol && lonCol) {
      const pts: GeoPoint[] = [];
      for (const row of rows) {
        const lat = Number(row[latCol]);
        const lon = Number(row[lonCol]);
        if (MapViewComponent.validLatLon(lat, lon)) {
          const props = { ...row };
          delete props[latCol];
          delete props[lonCol];
          pts.push({ lat, lon, props });
        }
      }
      if (pts.length) return pts;
    }

    return [];
  }

  private static parseGeoJSON(value: any): any | null {
    let obj = value;
    if (typeof value === 'string') {
      const s = value.trim();
      if (!s.startsWith('{') || s.indexOf('coordinates') === -1) return null;
      try {
        obj = JSON.parse(s);
      } catch {
        return null;
      }
    }
    if (obj && typeof obj === 'object' && obj.type && obj.coordinates) return obj;
    return null;
  }

  /** Rough centroid of a GeoJSON geometry — good enough for plotting a marker. */
  private static geoJsonCentroid(geo: any): { lat: number; lon: number } | null {
    if (!geo) return null;
    const coords: number[][] = [];
    const collect = (c: any) => {
      if (typeof c[0] === 'number' && typeof c[1] === 'number') {
        coords.push(c as number[]);
      } else if (Array.isArray(c)) {
        c.forEach(collect);
      }
    };
    collect(geo.coordinates);
    if (!coords.length) return null;
    let sx = 0;
    let sy = 0;
    for (const [lon, lat] of coords) {
      sx += lon;
      sy += lat;
    }
    const lon = sx / coords.length;
    const lat = sy / coords.length;
    return MapViewComponent.validLatLon(lat, lon) ? { lat, lon } : null;
  }

  private static validLatLon(lat: number, lon: number): boolean {
    return (
      Number.isFinite(lat) &&
      Number.isFinite(lon) &&
      lat >= -90 &&
      lat <= 90 &&
      lon >= -180 &&
      lon <= 180
    );
  }
}
