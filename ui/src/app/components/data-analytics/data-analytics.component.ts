import {
  Component, OnInit, OnDestroy, ViewChild, ElementRef, AfterViewInit,
  Output, EventEmitter, ChangeDetectionStrategy, ChangeDetectorRef
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatIconModule } from '@angular/material/icon';
import { MatButtonModule } from '@angular/material/button';
import { MatTooltipModule } from '@angular/material/tooltip';
import { MatSnackBar, MatSnackBarModule } from '@angular/material/snack-bar';
import { Chart, ChartConfiguration, ChartType, registerables } from 'chart.js';
import { ComponentStateService } from '../../services/component-state.service';
import { ThemeService } from '../../services/theme.service';
import { ApiService } from '../../services/api.service';
import { MapViewComponent } from '../shared/map-view/map-view.component';

Chart.register(...registerables);

type VizType = 'bar' | 'line' | 'pie' | 'doughnut' | 'horizontalBar' | 'area';

interface ColumnStats {
  column: string;
  type: 'numeric' | 'string' | 'date' | 'boolean';
  count: number;
  unique: number;
  nullCount: number;
  min?: number;
  max?: number;
  avg?: number;
  sum?: number;
  topValues?: { value: string; count: number }[];
}

@Component({
  selector: 'app-data-analytics',
  standalone: true,
  imports: [CommonModule, FormsModule, MatIconModule, MatButtonModule, MatTooltipModule, MatSnackBarModule, MapViewComponent],
  templateUrl: './data-analytics.component.html',
  styleUrls: ['./data-analytics.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class DataAnalyticsComponent implements OnInit, AfterViewInit, OnDestroy {
  @Output() goBack = new EventEmitter<void>();
  @ViewChild('mainChart') mainChartRef!: ElementRef<HTMLCanvasElement>;

  data: Record<string, any>[] = [];
  columns: string[] = [];
  numericColumns: string[] = [];
  stringColumns: string[] = [];
  userQuery = '';
  generatedSql = '';
  editableSql = '';
  sessionId = '';
  queryRunning = false;
  queryError = '';
  private autoRunOnInit = false;
  phaseTimings: { phase: string; duration_ms: number; duration_formatted: string }[] = [];
  totalTimeMs = 0;
  totalTimeFormatted = '';
  Math = Math;

  // Chart config
  mainChartType: VizType = 'area';
  labelColumn = '';
  valueColumn = '';

  dataLimit = 20;
  colorTheme = 'blue';

  // Stats
  columnStats: ColumnStats[] = [];
  totalRows = 0;
  totalColumns = 0;

  // Data preview pagination
  previewPage = 1;
  previewPageSize = 25;
  previewPageSizes = [10, 25, 50, 100];

  // Charts
  private mainChart: Chart | null = null;
  private chartsInitialized = false;

  chartTypes = [
    { value: 'bar' as VizType, label: 'Bar', icon: 'bar_chart' },
    { value: 'horizontalBar' as VizType, label: 'H-Bar', icon: 'align_horizontal_left' },
    { value: 'line' as VizType, label: 'Line', icon: 'show_chart' },
    { value: 'area' as VizType, label: 'Area', icon: 'area_chart' },
    { value: 'pie' as VizType, label: 'Pie', icon: 'pie_chart' },
    { value: 'doughnut' as VizType, label: 'Donut', icon: 'donut_large' },
  ];

  private themeColors: Record<string, string[]> = {
    blue: ['rgba(59,130,246,0.8)', 'rgba(37,99,235,0.8)', 'rgba(96,165,250,0.8)', 'rgba(29,78,216,0.8)', 'rgba(147,197,253,0.8)'],
    green: ['rgba(16,185,129,0.8)', 'rgba(5,150,105,0.8)', 'rgba(52,211,153,0.8)', 'rgba(4,120,87,0.8)', 'rgba(110,231,183,0.8)'],
    purple: ['rgba(139,92,246,0.8)', 'rgba(109,40,217,0.8)', 'rgba(167,139,250,0.8)', 'rgba(91,33,182,0.8)', 'rgba(196,181,253,0.8)'],
    rainbow: ['rgba(239,68,68,0.8)', 'rgba(245,158,11,0.8)', 'rgba(16,185,129,0.8)', 'rgba(59,130,246,0.8)', 'rgba(139,92,246,0.8)', 'rgba(236,72,153,0.8)', 'rgba(14,165,233,0.8)', 'rgba(34,197,94,0.8)'],
  };

  colorThemes = [
    { name: 'blue', preview: 'linear-gradient(135deg, #3B82F6, #1D4ED8)' },
    { name: 'green', preview: 'linear-gradient(135deg, #10B981, #059669)' },
    { name: 'purple', preview: 'linear-gradient(135deg, #8B5CF6, #6D28D9)' },
    { name: 'rainbow', preview: 'linear-gradient(135deg, #EF4444, #F59E0B, #10B981, #3B82F6)' },
  ];

  constructor(
    private componentState: ComponentStateService,
    private apiService: ApiService,
    private snackBar: MatSnackBar,
    private cdr: ChangeDetectorRef,
    public themeService: ThemeService
  ) {}

  ngOnInit(): void {
    // Subscribe to theme changes for instant toggle
    this.themeService.darkMode$.subscribe(() => this.cdr.markForCheck());

    const ctx = this.componentState.restoreState<{
      data: Record<string, any>[];
      columns: string[];
      userQuery: string;
      sql: string;
      sessionId: string;
      autoRun?: boolean;
      phaseTimings?: { phase: string; duration_ms: number; duration_formatted: string }[];
      totalTimeMs?: number;
      totalTimeFormatted?: string;
    }>('analytics-data');

    if (ctx) {
      this.userQuery = ctx.userQuery || '';
      this.generatedSql = ctx.sql || '';
      this.editableSql = this.generatedSql;
      this.sessionId = ctx.sessionId || '';
      this.autoRunOnInit = ctx.autoRun || false;
      this.phaseTimings = ctx.phaseTimings || [];
      this.totalTimeMs = ctx.totalTimeMs || 0;
      this.totalTimeFormatted = ctx.totalTimeFormatted || '';

      if (ctx.data?.length) {
        this.data = ctx.data;
        this.columns = ctx.columns || Object.keys(ctx.data[0]);
        this.totalRows = this.data.length;
        this.totalColumns = this.columns.length;
        this.detectColumnTypes();
        this.autoSelectColumns();
        this.computeStats();
      }
    }
  }

  ngAfterViewInit(): void {
    if (this.data.length) {
      this.chartsInitialized = true;
      setTimeout(() => {
        this.renderMainChart();
      }, 150);
    } else if (this.autoRunOnInit && this.editableSql) {
      this.chartsInitialized = true;
      setTimeout(() => this.runEditedQuery(), 200);
    }
  }

  ngOnDestroy(): void {
    this.mainChart?.destroy();
  }

  /** True when the current result set contains plottable geometry (PostGIS
   *  ST_AsGeoJSON output or a lat/lon column pair). Gates the map panel. */
  get hasGeoData(): boolean {
    return MapViewComponent.hasGeometry(this.data);
  }

  // ─── Column Detection ──────────────────────────────────
  private detectColumnTypes(): void {
    if (!this.data.length) return;
    const sample = this.data.slice(0, 20);
    this.numericColumns = [];
    this.stringColumns = [];

    for (const col of this.columns) {
      const vals = sample.map(r => r[col]).filter(v => v != null);
      const numCount = vals.filter(v => typeof v === 'number' || (!isNaN(parseFloat(v)) && isFinite(v))).length;
      if (numCount > vals.length * 0.6) {
        this.numericColumns.push(col);
      } else {
        this.stringColumns.push(col);
      }
    }
  }

  private autoSelectColumns(): void {
    if (this.stringColumns.length) this.labelColumn = this.stringColumns[0];
    else if (this.columns.length) this.labelColumn = this.columns[0];

    if (this.numericColumns.length) {
      this.valueColumn = this.numericColumns[0];
    } else {
      // No numeric columns — default to frequency count mode
      this.valueColumn = '__count__';
    }
  }

  private computeStats(): void {
    this.columnStats = this.columns.map(col => {
      const values = this.data.map(r => r[col]);
      const nonNull = values.filter(v => v != null && v !== '');
      const unique = new Set(nonNull.map(String)).size;
      const isNumeric = this.numericColumns.includes(col);

      const stat: ColumnStats = {
        column: col,
        type: isNumeric ? 'numeric' : 'string',
        count: nonNull.length,
        unique,
        nullCount: values.length - nonNull.length,
      };

      if (isNumeric) {
        const nums = nonNull.map(v => typeof v === 'number' ? v : parseFloat(v)).filter(n => !isNaN(n));
        if (nums.length) {
          stat.min = Math.min(...nums);
          stat.max = Math.max(...nums);
          stat.sum = nums.reduce((a, b) => a + b, 0);
          stat.avg = stat.sum / nums.length;
        }
      } else {
        // Top values for string columns
        const freq: Record<string, number> = {};
        nonNull.forEach(v => { freq[String(v)] = (freq[String(v)] || 0) + 1; });
        stat.topValues = Object.entries(freq)
          .sort((a, b) => b[1] - a[1])
          .slice(0, 5)
          .map(([value, count]) => ({ value, count }));
      }
      return stat;
    });
  }

  // ─── Chart Rendering ──────────────────────────────────
  private getColors(count: number): string[] {
    const base = this.themeColors[this.colorTheme] || this.themeColors['blue'];
    return Array.from({ length: count }, (_, i) => base[i % base.length]);
  }

  renderMainChart(): void {
    if (!this.mainChartRef?.nativeElement || !this.labelColumn || !this.valueColumn) return;
    this.mainChart?.destroy();

    let labels: string[];
    let values: number[];

    if (this.valueColumn === '__count__') {
      // Frequency count mode: aggregate label values
      const freq: Record<string, number> = {};
      this.data.forEach(r => {
        const key = String(r[this.labelColumn] ?? '').substring(0, 30);
        freq[key] = (freq[key] || 0) + 1;
      });
      const entries = Object.entries(freq)
        .sort((a, b) => b[1] - a[1])
        .slice(0, this.dataLimit);
      labels = entries.map(e => e[0]);
      values = entries.map(e => e[1]);
    } else {
      const limited = this.data.slice(0, this.dataLimit);
      labels = limited.map(r => String(r[this.labelColumn] ?? '').substring(0, 30));
      values = limited.map(r => {
        const v = r[this.valueColumn];
        return typeof v === 'number' ? v : parseFloat(v) || 0;
      });
    }

    let chartType: ChartType = 'bar';
    let indexAxis: 'x' | 'y' = 'x';
    let fill = false;
    switch (this.mainChartType) {
      case 'horizontalBar': chartType = 'bar'; indexAxis = 'y'; break;
      case 'line': chartType = 'line'; break;
      case 'area': chartType = 'line'; fill = true; break;
      case 'pie': chartType = 'pie'; break;
      case 'doughnut': chartType = 'doughnut'; break;
      default: chartType = 'bar';
    }

    const isPie = chartType === 'pie' || chartType === 'doughnut';
    const colors = this.getColors(values.length);

    this.mainChart = new Chart(this.mainChartRef.nativeElement, {
      type: chartType,
      data: {
        labels,
        datasets: [{
          label: this.valueColumn,
          data: values,
          backgroundColor: isPie ? colors : (fill ? colors[0].replace('0.8', '0.25') : colors[0]),
          borderColor: isPie ? colors.map(c => c.replace('0.8', '1')) : colors[0].replace('0.8', '1'),
          borderWidth: 2,
          borderRadius: chartType === 'bar' ? 6 : 0,
          fill,
          tension: 0.4,
          pointRadius: chartType === 'line' ? 4 : 0,
          pointHoverRadius: 6,
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        indexAxis,
        plugins: {
          legend: { display: isPie, position: 'right', labels: { boxWidth: 12, font: { size: 11 }, usePointStyle: true } },
          tooltip: { backgroundColor: 'rgba(17,24,39,0.9)', titleFont: { size: 12 }, bodyFont: { size: 11 }, padding: 10, cornerRadius: 8 }
        },
        scales: isPie ? {} : {
          x: { grid: { display: false }, ticks: { font: { size: 10 }, maxRotation: 45 } },
          y: { grid: { color: 'rgba(0,0,0,0.05)' }, ticks: { font: { size: 10 } } }
        }
      }
    });
  }

  // ─── User Actions ──────────────────────────────────────
  onMainConfigChange(): void {
    if (this.chartsInitialized) this.renderMainChart();
  }

  setTheme(theme: string): void {
    this.colorTheme = theme;
    if (this.chartsInitialized) {
      this.renderMainChart();
    }
  }

  setMainChartType(type: VizType): void {
    this.mainChartType = type;
    if (this.chartsInitialized) this.renderMainChart();
  }

  // ─── Run Edited Query ─────────────────────────────────
  runEditedQuery(): void {
    const sql = this.editableSql?.trim();
    if (!sql) return;
    this.queryRunning = true;
    this.queryError = '';
    this.cdr.markForCheck();

    this.apiService.executeDirectSQL(sql, 500, 0, true, this.sessionId || null).subscribe({
      next: (res) => {
        const rows: Record<string, any>[] = res.data || res.records || [];
        if (!rows.length) {
          this.queryError = 'Query returned no results.';
          this.queryRunning = false;
          this.cdr.markForCheck();
          return;
        }
        // Update all analytics state
        this.data = rows;
        this.columns = Object.keys(rows[0]);
        this.generatedSql = sql;
        this.totalRows = this.data.length;
        this.totalColumns = this.columns.length;
        this.detectColumnTypes();
        this.autoSelectColumns();
        this.computeStats();
        this.previewPage = 1;
        this.queryRunning = false;
        this.cdr.markForCheck();
        // Wait for Angular to render the canvas elements after data.length becomes truthy
        setTimeout(() => {
          this.renderMainChart();
          this.snackBar.open('Analytics updated', '', { duration: 2000 });
          this.cdr.markForCheck();
        }, 100);
      },
      error: (err) => {
        this.queryError = err?.error?.error || err?.error?.detail || err?.message || 'Query execution failed';
        this.queryRunning = false;
        this.cdr.markForCheck();
      }
    });
  }

  // ─── Download ──────────────────────────────────────────
  downloadCSV(): void {
    if (!this.data.length) return;
    const header = this.columns.join(',');
    const rows = this.data.map(r =>
      this.columns.map(c => {
        const v = r[c];
        if (v == null) return '';
        const s = String(v);
        return s.includes(',') || s.includes('"') || s.includes('\n')
          ? `"${s.replace(/"/g, '""')}"` : s;
      }).join(',')
    );
    const csv = [header, ...rows].join('\n');
    this.downloadFile(csv, 'data-export.csv', 'text/csv');
    this.snackBar.open('CSV downloaded', '', { duration: 2000 });
  }

  downloadExcel(): void {
    if (!this.data.length) return;
    // Generate a simple XML spreadsheet (Excel-compatible)
    let xml = '<?xml version="1.0"?><?mso-application progid="Excel.Sheet"?>';
    xml += '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"';
    xml += ' xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">';
    xml += '<Worksheet ss:Name="Data"><Table>';
    // Header row
    xml += '<Row>';
    this.columns.forEach(c => { xml += `<Cell><Data ss:Type="String">${this.escapeXml(c)}</Data></Cell>`; });
    xml += '</Row>';
    // Data rows
    this.data.forEach(r => {
      xml += '<Row>';
      this.columns.forEach(c => {
        const v = r[c];
        const isNum = typeof v === 'number';
        xml += `<Cell><Data ss:Type="${isNum ? 'Number' : 'String'}">${this.escapeXml(String(v ?? ''))}</Data></Cell>`;
      });
      xml += '</Row>';
    });
    xml += '</Table></Worksheet></Workbook>';
    this.downloadFile(xml, 'data-export.xls', 'application/vnd.ms-excel');
    this.snackBar.open('Excel file downloaded', '', { duration: 2000 });
  }

  private escapeXml(s: string): string {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  private downloadFile(content: string, filename: string, mimeType: string): void {
    const blob = new Blob([content], { type: mimeType + ';charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  onBack(): void {
    this.goBack.emit();
  }

  // ── Data Preview Pagination ───────────────────────────────────
  get previewTotalPages(): number {
    return Math.ceil(this.data.length / this.previewPageSize);
  }

  get previewStartRow(): number {
    return (this.previewPage - 1) * this.previewPageSize + 1;
  }

  get previewEndRow(): number {
    return Math.min(this.previewPage * this.previewPageSize, this.data.length);
  }

  get paginatedData(): Record<string, any>[] {
    const start = (this.previewPage - 1) * this.previewPageSize;
    return this.data.slice(start, start + this.previewPageSize);
  }

  onPreviewPageSizeChange(size: number): void {
    this.previewPageSize = size;
    this.previewPage = 1;
    this.cdr.markForCheck();
  }

  onPreviewFirstPage(): void { this.previewPage = 1; this.cdr.markForCheck(); }
  onPreviewPrevPage(): void { if (this.previewPage > 1) { this.previewPage--; this.cdr.markForCheck(); } }
  onPreviewNextPage(): void { if (this.previewPage < this.previewTotalPages) { this.previewPage++; this.cdr.markForCheck(); } }
  onPreviewLastPage(): void { this.previewPage = this.previewTotalPages; this.cdr.markForCheck(); }
}
