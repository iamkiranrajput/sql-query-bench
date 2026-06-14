import { Component, Input, Output, EventEmitter, OnChanges, SimpleChanges, OnInit, OnDestroy, ViewChild, ElementRef, AfterViewInit } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { MatTooltipModule } from '@angular/material/tooltip';
import { ApiService } from '../../services/api.service';
import { ComponentStateService } from '../../services/component-state.service';
import { SchemaStatusIndicatorComponent } from '../shared/schema-status-indicator/schema-status-indicator.component';

interface TableSchema {
  table_name: string;
  columns: ColumnInfo[];
  row_count?: number;
  ai_description?: string;
  expanded?: boolean;
  loadingDescription?: boolean;
}

interface ColumnInfo {
  column_name: string;
  data_type: string;
  is_nullable: boolean | string;
  column_default?: string;
}

interface SchemaData {
  tables: TableSchema[];
}

// Visual schema interfaces
interface VisualColumn {
  name: string;
  data_type: string;
  is_nullable: boolean;
  is_pk: boolean;
  is_fk: boolean;
  fk_target_table: string | null;
  fk_target_column: string | null;
  default_value: string | null;
}

interface VisualTable {
  name: string;
  columns: VisualColumn[];
  row_count: number | null;
  column_count: number;
  expanded: boolean;
}

interface VisualRelationship {
  from_table: string;
  from_column: string;
  to_table: string;
  to_column: string;
  relationship_type: string;
  confidence: number;
  method: string;
}

interface VisualSchemaData {
  tables: VisualTable[];
  relationships: VisualRelationship[];
  total_tables: number;
  total_columns: number;
  total_relationships: number;
}

// ERD canvas position tracking
interface TablePosition {
  x: number;
  y: number;
  width: number;
  height: number;
}

interface RelationshipLine {
  id: string;
  fromTable: string;
  fromColumn: string;
  toTable: string;
  toColumn: string;
  path: string;
  labelX: number;
  labelY: number;
  method: string;
  confidence: number;
  color: string;
}

interface SavedSchemaExplorerState {
  searchTerm: string;
  expandedTables: string[];
  selectedTableName: string | null;
  schemaData: SchemaData | null;
  scrollPosition: number;
  viewMode: 'list' | 'visual';
}

@Component({
  selector: 'app-schema-explorer',
  standalone: true,
  imports: [CommonModule, FormsModule, MatIconModule, MatProgressSpinnerModule, MatTooltipModule, SchemaStatusIndicatorComponent],
  templateUrl: './schema-explorer.component.html',
  styleUrl: './schema-explorer.component.scss'
})
export class SchemaExplorerComponent implements OnChanges, OnInit, OnDestroy, AfterViewInit {
  @Input() sessionId: string | null = null;
  @Output() queryTable = new EventEmitter<{
    tableName: string;
    connectedTables: string[];
    relationships: { from: string; to: string; fromCol: string; toCol: string; method: string }[];
    columns: { name: string; type: string; isPk: boolean; isFk: boolean }[];
    connectedTableColumns: { [table: string]: { name: string; type: string; isPk: boolean; isFk: boolean }[] };
  }>();
  @ViewChild('scrollContainer') scrollContainer!: ElementRef<HTMLDivElement>;
  private readonly STATE_KEY = 'schema-explorer';

  schemaData: SchemaData | null = null;
  loading = false;
  error: string | null = null;
  searchTerm = '';
  selectedTable: TableSchema | null = null;
  tableDescription: string | null = null;
  loadingTableDescription = false;
  private lastSearchTerm = '';
  private scrollRestored = false;

  // View mode: 'list' (tree view) or 'visual' (ERD-style cards)
  viewMode: 'list' | 'visual' = 'list';
  visualData: VisualSchemaData | null = null;
  visualLoading = false;
  visualError: string | null = null;
  visualSearchTerm = '';
  refreshingFkGraph = false;
  showInferredRelationships = true;
  selectedRelationship: VisualRelationship | null = null;

  // ERD Canvas state
  @ViewChild('erdCanvas') erdCanvas!: ElementRef<HTMLDivElement>;
  @ViewChild('erdSvg') erdSvg!: ElementRef<SVGSVGElement>;
  tablePositions: Map<string, TablePosition> = new Map();
  relationshipLines: RelationshipLine[] = [];
  erdZoom = 1;
  erdPanX = 0;
  erdPanY = 0;
  private isPanning = false;
  private panStartX = 0;
  private panStartY = 0;
  private panStartPanX = 0;
  private panStartPanY = 0;
  private isDragging = false;
  private dragTable: string | null = null;
  private dragOffsetX = 0;
  private dragOffsetY = 0;
  private erdMouseMoveListener: ((e: MouseEvent) => void) | null = null;
  private erdMouseUpListener: ((e: MouseEvent) => void) | null = null;
  highlightedTable: string | null = null;
  canvasWidth = 4000;
  canvasHeight = 3000;
  miniMapScale = 0.04;
  showMiniMap = true;
  
  // Load all tables at once (no pagination)
  displayedTablesCount = Number.MAX_SAFE_INTEGER;

  constructor(
    private apiService: ApiService,
    private componentState: ComponentStateService
  ) {}

  ngOnInit(): void {
    // Restore state if available
    this.restoreState();
  }

  ngAfterViewInit(): void {
    // Restore scroll position after view is initialized
    setTimeout(() => {
      this.restoreScrollPosition();
    }, 100);
    
    // Add scroll listener to save position as user scrolls
    if (this.scrollContainer?.nativeElement) {
      this.scrollContainer.nativeElement.addEventListener('scroll', () => {
        this.saveScrollPosition();
      });
    }
  }

  ngOnDestroy(): void {
    // Save scroll position before component is destroyed
    this.saveScrollPosition();
    // Save state before component is destroyed
    this.saveState();
    // Clean up ERD event listeners
    if (this.erdMouseMoveListener) {
      document.removeEventListener('mousemove', this.erdMouseMoveListener);
    }
    if (this.erdMouseUpListener) {
      document.removeEventListener('mouseup', this.erdMouseUpListener);
    }
  }

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['sessionId']) {
      if (this.sessionId) {
        // Only load schema if we don't have cached data or session changed
        const savedState = this.componentState.restoreState<SavedSchemaExplorerState>(this.getStateKey());
        if (!savedState || !savedState.schemaData) {
          this.loadSchema();
        } else {
          // Restore from saved state
          this.restoreState();
        }
      } else {
        // Session cleared, clear state
        this.componentState.clearState(this.getStateKey());
      }
    }
  }

  async loadSchema(): Promise<void> {
    if (!this.sessionId) return;
    
    this.loading = true;
    this.error = null;
    
    try {
      const response = await this.apiService.getSchemaExplorer(this.sessionId).toPromise();
      if (response?.success) {
        // Restore expanded state from saved state
        const savedState = this.componentState.restoreState<SavedSchemaExplorerState>(this.getStateKey());
        const expandedTables = savedState?.expandedTables || [];
        
        // Transform backend response to match frontend interface
        this.schemaData = {
          tables: response.tables.map((t: any) => ({
            table_name: t.name,
            columns: (t.columns || []).map((c: any) => ({
              column_name: c.name,
              data_type: c.type,
              is_nullable: c.nullable,
              column_default: c.default
            })),
            row_count: t.row_count,
            ai_description: t.ai_description,
            expanded: expandedTables.includes(t.name),
            loadingDescription: false
          }))
        };
        
        // Restore selected table if it exists
        if (savedState?.selectedTableName) {
          const table = this.schemaData.tables.find(t => t.table_name === savedState.selectedTableName);
          if (table) {
            this.selectedTable = table;
            if (table.ai_description) {
              this.tableDescription = table.ai_description;
            }
          }
        }
        
        // All tables are displayed at once (no pagination reset needed)
        
        // Save state after loading
        this.saveState();
      } else {
        this.error = response?.error || 'Failed to load schema';
      }
    } catch (err: any) {
      this.error = err.message || 'Failed to load database schema';
    } finally {
      this.loading = false;
    }
  }

  get filteredTables(): TableSchema[] {
    if (!this.schemaData?.tables) return [];
    
    const prevSearchTerm = this.lastSearchTerm;
    const currentSearchTerm = this.searchTerm.trim();
    
    if (!currentSearchTerm) {
      this.lastSearchTerm = '';
      // Return all tables when no search term
      return this.schemaData.tables;
    }
    
    const term = currentSearchTerm.toLowerCase();
    const filtered = this.schemaData.tables.filter(table => 
      table.table_name.toLowerCase().includes(term) ||
      table.columns.some(col => col.column_name.toLowerCase().includes(term))
    );
    
    // Auto-expand tables that have matching columns when search term changes
    if (term && term !== this.lastSearchTerm) {
      filtered.forEach(table => {
        const hasMatchingColumn = table.columns.some(col => 
          col.column_name.toLowerCase().includes(term)
        );
        if (hasMatchingColumn && !table.expanded) {
          table.expanded = true;
        }
      });
      this.lastSearchTerm = term;
      // Save state after auto-expansion
      this.saveState();
    }
    
    // When searching, show all filtered results (usually much smaller)
    return filtered;
  }
  
  // Pagination removed - all tables load at once
  // These methods kept for backward compatibility but no longer used
  get hasMoreTables(): boolean {
    return false; // Always show all tables
  }
  
  loadMoreTables(): void {
    // No-op: All tables are already displayed
  }
  
  resetPagination(): void {
    // No-op: All tables are always displayed
  }

  /**
   * Check if a column name matches the current search term
   */
  isColumnMatch(columnName: string): boolean {
    if (!this.searchTerm.trim()) return false;
    return columnName.toLowerCase().includes(this.searchTerm.toLowerCase());
  }

  /**
   * Get count of matching columns in a table
   */
  getMatchingColumnCount(table: TableSchema): number {
    if (!this.searchTerm.trim()) return 0;
    const term = this.searchTerm.toLowerCase();
    return table.columns.filter(col => 
      col.column_name.toLowerCase().includes(term)
    ).length;
  }

  /**
   * Check if table name matches search term
   */
  isTableMatch(tableName: string): boolean {
    if (!this.searchTerm.trim()) return false;
    return tableName.toLowerCase().includes(this.searchTerm.toLowerCase());
  }

  toggleTable(table: TableSchema): void {
    table.expanded = !table.expanded;
    // Save state when table expansion changes
    this.saveState();
  }

  async getTableDescription(table: TableSchema): Promise<void> {
    if (!this.sessionId) return;
    
    this.selectedTable = table;
    this.loadingTableDescription = true;
    this.tableDescription = null;
    
    // Save state when selecting a table
    this.saveState();
    
    try {
      const response = await this.apiService.describeTable(table.table_name, this.sessionId).toPromise();
      if (response?.success) {
        this.tableDescription = response.description;
        // Update table description in schema data
        if (this.schemaData) {
          const tableInData = this.schemaData.tables.find(t => t.table_name === table.table_name);
          if (tableInData) {
            tableInData.ai_description = response.description;
            this.saveState();
          }
        }
      } else {
        this.tableDescription = 'Unable to generate description.';
      }
    } catch (err: any) {
      this.tableDescription = 'Error: ' + (err.message || 'Failed to get table description');
    } finally {
      this.loadingTableDescription = false;
    }
  }

  closeTableDetails(): void {
    this.selectedTable = null;
    this.tableDescription = null;
    // Save state when closing details
    this.saveState();
  }

  getDataTypeIcon(dataType: string): string {
    const type = dataType.toLowerCase();
    if (type.includes('int') || type.includes('numeric') || type.includes('decimal') || type.includes('float') || type.includes('double')) {
      return 'tag';
    }
    if (type.includes('varchar') || type.includes('text') || type.includes('char')) {
      return 'text_fields';
    }
    if (type.includes('date') || type.includes('time')) {
      return 'event';
    }
    if (type.includes('bool')) {
      return 'toggle_on';
    }
    if (type.includes('json')) {
      return 'data_object';
    }
    return 'code';
  }

  getDataTypeColor(dataType: string): string {
    const type = dataType.toLowerCase();
    if (type.includes('int') || type.includes('numeric') || type.includes('decimal') || type.includes('float') || type.includes('double')) {
      return 'text-blue-600';
    }
    if (type.includes('varchar') || type.includes('text') || type.includes('char')) {
      return 'text-green-600';
    }
    if (type.includes('date') || type.includes('time')) {
      return 'text-purple-600';
    }
    if (type.includes('bool')) {
      return 'text-amber-600';
    }
    if (type.includes('json')) {
      return 'text-orange-600';
    }
    return 'text-gray-600';
  }

  refreshSchema(): void {
    // Clear saved state and reload
    this.componentState.clearState(this.getStateKey());
    if (this.viewMode === 'visual') {
      this.loadVisualSchema();
    } else {
      this.loadSchema();
    }
  }

  // ====================================================================
  // Visual Structure Mode — ERD Canvas
  // ====================================================================

  toggleViewMode(): void {
    this.viewMode = this.viewMode === 'list' ? 'visual' : 'list';
    if (this.viewMode === 'visual' && !this.visualData) {
      this.loadVisualSchema();
    }
    this.saveState();
  }

  async loadVisualSchema(): Promise<void> {
    if (!this.sessionId) return;

    this.visualLoading = true;
    this.visualError = null;

    try {
      const response = await this.apiService.getSchemaVisual(this.sessionId).toPromise();
      if (response?.success) {
        this.visualData = {
          tables: (response.tables || []).map((t: any) => ({
            ...t,
            expanded: true
          })),
          relationships: response.relationships || [],
          total_tables: response.total_tables || 0,
          total_columns: response.total_columns || 0,
          total_relationships: response.total_relationships || 0,
        };
        // Auto-layout after data loads
        setTimeout(() => this.autoLayoutTables(), 50);
      } else {
        this.visualError = response?.error || 'Failed to load visual schema';
      }
    } catch (err: any) {
      this.visualError = err.message || 'Failed to load visual schema structure';
    } finally {
      this.visualLoading = false;
    }
  }

  toggleVisualTable(table: VisualTable): void {
    table.expanded = !table.expanded;
    // Recalculate lines after expansion change
    setTimeout(() => this.recalculateLines(), 50);
  }

  async regenerateFkGraph(): Promise<void> {
    if (!this.sessionId || this.refreshingFkGraph) return;
    this.refreshingFkGraph = true;
    try {
      const resp = await this.apiService.refreshFkGraph(this.sessionId).toPromise();
      if (resp?.success) {
        // Reload visual schema with the new FK graph data
        await this.loadVisualSchema();
      }
    } catch (err: any) {
      console.error('FK graph regeneration failed:', err);
    } finally {
      this.refreshingFkGraph = false;
    }
  }

  get filteredVisualTables(): VisualTable[] {
    if (!this.visualData?.tables) return [];
    if (!this.visualSearchTerm.trim()) return this.visualData.tables;
    const term = this.visualSearchTerm.toLowerCase();
    return this.visualData.tables.filter(t =>
      t.name.toLowerCase().includes(term) ||
      t.columns.some(c => c.name.toLowerCase().includes(term))
    );
  }

  getRelationshipsForTable(tableName: string): VisualRelationship[] {
    if (!this.visualData?.relationships) return [];
    return this.visualData.relationships.filter(r =>
      r.from_table.toLowerCase() === tableName.toLowerCase() ||
      r.to_table.toLowerCase() === tableName.toLowerCase()
    );
  }

  get explicitRelCount(): number {
    return this.visualData?.relationships?.filter(r => r.method === 'explicit').length || 0;
  }

  get inferredRelCount(): number {
    return this.visualData?.relationships?.filter(r => r.method !== 'explicit').length || 0;
  }

  getOutgoingRelationships(tableName: string): VisualRelationship[] {
    if (!this.visualData?.relationships) return [];
    return this.visualData.relationships.filter(r =>
      r.from_table.toLowerCase() === tableName.toLowerCase()
    );
  }

  getIncomingRelationships(tableName: string): VisualRelationship[] {
    if (!this.visualData?.relationships) return [];
    return this.visualData.relationships.filter(r =>
      r.to_table.toLowerCase() === tableName.toLowerCase()
    );
  }

  getPkColumns(table: VisualTable): VisualColumn[] {
    return table.columns.filter(c => c.is_pk);
  }

  getFkColumns(table: VisualTable): VisualColumn[] {
    return table.columns.filter(c => c.is_fk);
  }

  /**
   * Open chat query page pre-loaded with this table and all its connected tables
   */
  openChatForTable(tableName: string): void {
    const rels = this.getRelationshipsForTable(tableName);
    const connectedSet = new Set<string>();
    const relDetails: { from: string; to: string; fromCol: string; toCol: string; method: string }[] = [];

    for (const r of rels) {
      const other = r.from_table.toLowerCase() === tableName.toLowerCase() ? r.to_table : r.from_table;
      connectedSet.add(other);
      relDetails.push({
        from: r.from_table,
        to: r.to_table,
        fromCol: r.from_column,
        toCol: r.to_column,
        method: r.method
      });
    }

    // Gather columns for main table
    const mainTable = this.visualData?.tables.find(t => t.name.toLowerCase() === tableName.toLowerCase());
    const columns = (mainTable?.columns || []).map(c => ({
      name: c.name, type: c.data_type, isPk: c.is_pk, isFk: c.is_fk
    }));

    // Gather columns for connected tables
    const connectedTableColumns: { [table: string]: { name: string; type: string; isPk: boolean; isFk: boolean }[] } = {};
    for (const ct of connectedSet) {
      const t = this.visualData?.tables.find(vt => vt.name.toLowerCase() === ct.toLowerCase());
      if (t) {
        connectedTableColumns[ct] = t.columns.map(c => ({
          name: c.name, type: c.data_type, isPk: c.is_pk, isFk: c.is_fk
        }));
      }
    }

    this.queryTable.emit({
      tableName,
      connectedTables: Array.from(connectedSet),
      relationships: relDetails,
      columns,
      connectedTableColumns
    });
  }

  getRegularColumns(table: VisualTable): VisualColumn[] {
    return table.columns.filter(c => !c.is_pk && !c.is_fk);
  }

  getConfidenceLabel(confidence: number): string {
    if (confidence >= 0.9) return 'High';
    if (confidence >= 0.6) return 'Medium';
    return 'Low';
  }

  getConfidenceClass(confidence: number): string {
    if (confidence >= 0.9) return 'confidence-high';
    if (confidence >= 0.6) return 'confidence-medium';
    return 'confidence-low';
  }

  trackByVisualTableName(index: number, table: VisualTable): string {
    return table.name;
  }

  collapseAllVisualTables(): void {
    if (this.visualData?.tables) {
      this.visualData.tables.forEach(t => t.expanded = false);
      setTimeout(() => this.recalculateLines(), 50);
    }
  }

  expandAllVisualTables(): void {
    if (this.visualData?.tables) {
      this.visualData.tables.forEach(t => t.expanded = true);
      setTimeout(() => this.recalculateLines(), 50);
    }
  }

  // ── Auto-layout algorithm (force-directed inspired) ──

  autoLayoutTables(): void {
    if (!this.visualData?.tables) return;

    const tables = this.visualData.tables;
    const rels = this.visualData.relationships;
    const CARD_WIDTH = 280;
    const ROW_HEIGHT = 24;
    const HEADER_HEIGHT = 40;
    const PADDING = 60;

    // Build adjacency for grouping related tables
    const adjacency: Map<string, Set<string>> = new Map();
    tables.forEach(t => adjacency.set(t.name.toLowerCase(), new Set()));
    rels.forEach(r => {
      const from = r.from_table.toLowerCase();
      const to = r.to_table.toLowerCase();
      adjacency.get(from)?.add(to);
      adjacency.get(to)?.add(from);
    });

    // Find connected components
    const visited = new Set<string>();
    const components: string[][] = [];
    tables.forEach(t => {
      const key = t.name.toLowerCase();
      if (!visited.has(key)) {
        const component: string[] = [];
        const queue = [key];
        while (queue.length > 0) {
          const node = queue.shift()!;
          if (visited.has(node)) continue;
          visited.add(node);
          component.push(node);
          adjacency.get(node)?.forEach(neighbor => {
            if (!visited.has(neighbor)) queue.push(neighbor);
          });
        }
        components.push(component);
      }
    });

    // Sort components: largest first
    components.sort((a, b) => b.length - a.length);

    // Calculate card height
    const getCardHeight = (t: VisualTable): number => {
      if (!t.expanded) return HEADER_HEIGHT + 10;
      return HEADER_HEIGHT + Math.min(t.columns.length, 15) * ROW_HEIGHT + 30;
    };

    // Layout each component in a cluster
    let globalOffsetY = PADDING;
    const tableMap = new Map<string, VisualTable>();
    tables.forEach(t => tableMap.set(t.name.toLowerCase(), t));

    components.forEach(component => {
      const count = component.length;
      const cols = Math.ceil(Math.sqrt(count));
      let maxRowHeight = 0;
      let cx = PADDING;
      let cy = globalOffsetY;
      let colIndex = 0;

      component.forEach((tKey, i) => {
        const table = tableMap.get(tKey);
        if (!table) return;

        const h = getCardHeight(table);
        this.tablePositions.set(table.name, { x: cx, y: cy, width: CARD_WIDTH, height: h });
        maxRowHeight = Math.max(maxRowHeight, h);
        colIndex++;

        if (colIndex >= cols) {
          colIndex = 0;
          cx = PADDING;
          cy += maxRowHeight + PADDING;
          maxRowHeight = 0;
        } else {
          cx += CARD_WIDTH + PADDING;
        }
      });

      globalOffsetY = cy + maxRowHeight + PADDING * 2;
    });

    // Calculate canvas size based on positions
    let maxX = 0, maxY = 0;
    this.tablePositions.forEach(pos => {
      maxX = Math.max(maxX, pos.x + pos.width + PADDING);
      maxY = Math.max(maxY, pos.y + pos.height + PADDING);
    });
    this.canvasWidth = Math.max(maxX + 200, 2000);
    this.canvasHeight = Math.max(maxY + 200, 1500);

    this.recalculateLines();
    // Center view
    this.erdZoom = 1;
    this.erdPanX = 0;
    this.erdPanY = 0;
  }

  recalculateLines(): void {
    if (!this.visualData) return;
    const lines: RelationshipLine[] = [];

    this.visualData.relationships.forEach((rel, idx) => {
      const fromPos = this.tablePositions.get(rel.from_table);
      const toPos = this.tablePositions.get(rel.to_table);
      if (!fromPos || !toPos) return;

      // Determine connection points (edge of cards)
      const fromCenterX = fromPos.x + fromPos.width / 2;
      const fromCenterY = fromPos.y + fromPos.height / 2;
      const toCenterX = toPos.x + toPos.width / 2;
      const toCenterY = toPos.y + toPos.height / 2;

      // Determine which sides to connect
      let startX: number, startY: number, endX: number, endY: number;
      const dx = toCenterX - fromCenterX;
      const dy = toCenterY - fromCenterY;

      if (Math.abs(dx) > Math.abs(dy)) {
        // Horizontal connection
        if (dx > 0) {
          startX = fromPos.x + fromPos.width;
          endX = toPos.x;
        } else {
          startX = fromPos.x;
          endX = toPos.x + toPos.width;
        }
        startY = fromCenterY;
        endY = toCenterY;
      } else {
        // Vertical connection
        startX = fromCenterX;
        endX = toCenterX;
        if (dy > 0) {
          startY = fromPos.y + fromPos.height;
          endY = toPos.y;
        } else {
          startY = fromPos.y;
          endY = toPos.y + toPos.height;
        }
      }

      // Build bezier curve path
      const midX = (startX + endX) / 2;
      const midY = (startY + endY) / 2;

      let path: string;
      if (Math.abs(dx) > Math.abs(dy)) {
        path = `M ${startX} ${startY} C ${midX} ${startY}, ${midX} ${endY}, ${endX} ${endY}`;
      } else {
        path = `M ${startX} ${startY} C ${startX} ${midY}, ${endX} ${midY}, ${endX} ${endY}`;
      }

      // Skip inferred lines when hidden
      const isInferred = rel.method !== 'explicit';
      if (isInferred && !this.showInferredRelationships) return;

      // Color based on method
      let color = '#8b5cf6'; // purple for explicit FK
      if (isInferred) {
        color = '#e85d04'; // orange for inferred (naming_pattern, naming_pattern_weak, learned, etc.)
        if (rel.method === 'usage') {
          color = '#10b981'; // green for usage-based
        }
      }

      lines.push({
        id: `rel-${idx}`,
        fromTable: rel.from_table,
        fromColumn: rel.from_column,
        toTable: rel.to_table,
        toColumn: rel.to_column,
        path,
        labelX: midX,
        labelY: midY,
        method: rel.method,
        confidence: rel.confidence,
        color,
      });
    });

    this.relationshipLines = lines;
  }

  // ── ERD Canvas interactions ──

  getTableStyle(tableName: string): { [key: string]: string } {
    const pos = this.tablePositions.get(tableName);
    if (!pos) return {};
    return {
      'left': `${pos.x}px`,
      'top': `${pos.y}px`,
      'width': `${pos.width}px`,
      'position': 'absolute'
    };
  }

  onCanvasMouseDown(event: MouseEvent): void {
    // Only start panning if clicking on canvas background (not a table)
    if ((event.target as HTMLElement).closest('.erd-table-card')) return;
    this.isPanning = true;
    this.panStartX = event.clientX;
    this.panStartY = event.clientY;
    this.panStartPanX = this.erdPanX;
    this.panStartPanY = this.erdPanY;
    event.preventDefault();

    this.erdMouseMoveListener = (e: MouseEvent) => this.onCanvasMouseMove(e);
    this.erdMouseUpListener = (e: MouseEvent) => this.onCanvasMouseUp(e);
    document.addEventListener('mousemove', this.erdMouseMoveListener);
    document.addEventListener('mouseup', this.erdMouseUpListener);
  }

  onCanvasMouseMove(event: MouseEvent): void {
    if (this.isPanning) {
      this.erdPanX = this.panStartPanX + (event.clientX - this.panStartX);
      this.erdPanY = this.panStartPanY + (event.clientY - this.panStartY);
    }
    if (this.isDragging && this.dragTable) {
      const pos = this.tablePositions.get(this.dragTable);
      if (pos) {
        const newX = (event.clientX - this.dragOffsetX - this.erdPanX) / this.erdZoom;
        const newY = (event.clientY - this.dragOffsetY - this.erdPanY) / this.erdZoom;
        this.tablePositions.set(this.dragTable, { ...pos, x: Math.max(0, newX), y: Math.max(0, newY) });
        this.recalculateLines();
      }
    }
  }

  onCanvasMouseUp(event: MouseEvent): void {
    this.isPanning = false;
    this.isDragging = false;
    this.dragTable = null;
    if (this.erdMouseMoveListener) {
      document.removeEventListener('mousemove', this.erdMouseMoveListener);
      this.erdMouseMoveListener = null;
    }
    if (this.erdMouseUpListener) {
      document.removeEventListener('mouseup', this.erdMouseUpListener);
      this.erdMouseUpListener = null;
    }
  }

  onTableDragStart(event: MouseEvent, tableName: string): void {
    event.stopPropagation();
    event.preventDefault();
    this.isDragging = true;
    this.dragTable = tableName;

    const pos = this.tablePositions.get(tableName);
    if (pos) {
      const canvasRect = this.erdCanvas?.nativeElement?.getBoundingClientRect();
      if (canvasRect) {
        this.dragOffsetX = event.clientX - (pos.x * this.erdZoom + this.erdPanX);
        this.dragOffsetY = event.clientY - (pos.y * this.erdZoom + this.erdPanY);
      }
    }

    this.erdMouseMoveListener = (e: MouseEvent) => this.onCanvasMouseMove(e);
    this.erdMouseUpListener = (e: MouseEvent) => this.onCanvasMouseUp(e);
    document.addEventListener('mousemove', this.erdMouseMoveListener);
    document.addEventListener('mouseup', this.erdMouseUpListener);
  }

  onCanvasWheel(event: WheelEvent): void {
    event.preventDefault();
    const delta = event.deltaY > 0 ? -0.08 : 0.08;
    const newZoom = Math.max(0.15, Math.min(2.5, this.erdZoom + delta));

    // Zoom toward mouse position
    const rect = this.erdCanvas?.nativeElement?.getBoundingClientRect();
    if (rect) {
      const mx = event.clientX - rect.left;
      const my = event.clientY - rect.top;
      this.erdPanX = mx - (mx - this.erdPanX) * (newZoom / this.erdZoom);
      this.erdPanY = my - (my - this.erdPanY) * (newZoom / this.erdZoom);
    }

    this.erdZoom = newZoom;
  }

  zoomIn(): void {
    this.erdZoom = Math.min(2.5, this.erdZoom + 0.15);
  }

  zoomOut(): void {
    this.erdZoom = Math.max(0.15, this.erdZoom - 0.15);
  }

  resetZoom(): void {
    this.erdZoom = 1;
    this.erdPanX = 0;
    this.erdPanY = 0;
  }

  /** Full reset: re-layout tables + reset zoom/pan to default */
  resetToOriginal(): void {
    this.erdZoom = 1;
    this.erdPanX = 0;
    this.erdPanY = 0;
    this.highlightedTable = null;
    this.autoLayoutTables();
  }

  toggleInferredRelationships(): void {
    this.showInferredRelationships = !this.showInferredRelationships;
    this.recalculateLines();
  }

  fitToScreen(): void {
    if (!this.erdCanvas?.nativeElement || !this.tablePositions.size) return;
    const rect = this.erdCanvas.nativeElement.getBoundingClientRect();
    
    let minX = Infinity, minY = Infinity, maxX = 0, maxY = 0;
    this.tablePositions.forEach(pos => {
      minX = Math.min(minX, pos.x);
      minY = Math.min(minY, pos.y);
      maxX = Math.max(maxX, pos.x + pos.width);
      maxY = Math.max(maxY, pos.y + pos.height);
    });

    const contentW = maxX - minX + 100;
    const contentH = maxY - minY + 100;
    const scaleX = rect.width / contentW;
    const scaleY = rect.height / contentH;
    this.erdZoom = Math.min(scaleX, scaleY, 1.2);
    this.erdPanX = (rect.width - contentW * this.erdZoom) / 2 - minX * this.erdZoom + 50;
    this.erdPanY = (rect.height - contentH * this.erdZoom) / 2 - minY * this.erdZoom + 50;
  }

  highlightTableRelationships(tableName: string): void {
    this.highlightedTable = tableName;
  }

  clearHighlight(): void {
    this.highlightedTable = null;
  }

  isLineHighlighted(line: RelationshipLine): boolean {
    if (!this.highlightedTable) return true;
    return line.fromTable.toLowerCase() === this.highlightedTable.toLowerCase() ||
           line.toTable.toLowerCase() === this.highlightedTable.toLowerCase();
  }

  getRelLineLabel(line: RelationshipLine): string {
    if (line.method === 'explicit') return 'FK';
    if (line.method === 'usage') return 'Usage';
    // All other methods are inferred variants
    return 'Inferred';
  }

  // Mini-map helpers
  getMiniMapTableStyle(tableName: string): { [key: string]: string } {
    const pos = this.tablePositions.get(tableName);
    if (!pos) return { display: 'none' };
    return {
      'left': `${pos.x * this.miniMapScale}px`,
      'top': `${pos.y * this.miniMapScale}px`,
      'width': `${pos.width * this.miniMapScale}px`,
      'height': `${pos.height * this.miniMapScale}px`,
      'position': 'absolute'
    };
  }

  getMiniMapViewportStyle(): { [key: string]: string } {
    if (!this.erdCanvas?.nativeElement) return {};
    const rect = this.erdCanvas.nativeElement.getBoundingClientRect();
    const vpW = rect.width / this.erdZoom;
    const vpH = rect.height / this.erdZoom;
    const vpX = -this.erdPanX / this.erdZoom;
    const vpY = -this.erdPanY / this.erdZoom;
    return {
      'left': `${vpX * this.miniMapScale}px`,
      'top': `${vpY * this.miniMapScale}px`,
      'width': `${vpW * this.miniMapScale}px`,
      'height': `${vpH * this.miniMapScale}px`,
      'position': 'absolute'
    };
  }

  private getStateKey(): string {
    return `${this.STATE_KEY}-${this.sessionId || 'no-session'}`;
  }
  
  // PERFORMANCE: TrackBy function for *ngFor to improve rendering performance
  trackByTableName(index: number, table: TableSchema): string {
    return table.table_name;
  }

  saveState(): void {
    if (!this.sessionId) return;
    
    const expandedTables = this.schemaData?.tables
      .filter(t => t.expanded)
      .map(t => t.table_name) || [];
    
    // Save scroll position
    const scrollPosition = this.getScrollPosition();
    
    const state: SavedSchemaExplorerState = {
      searchTerm: this.searchTerm,
      expandedTables: expandedTables,
      selectedTableName: this.selectedTable?.table_name || null,
      schemaData: this.schemaData,
      scrollPosition: scrollPosition,
      viewMode: this.viewMode
    };
    
    this.componentState.saveState(this.getStateKey(), state);
  }

  private getScrollPosition(): number {
    if (this.scrollContainer?.nativeElement) {
      return this.scrollContainer.nativeElement.scrollTop;
    }
    return 0;
  }

  private saveScrollPosition(): void {
    if (this.scrollContainer?.nativeElement) {
      const scrollTop = this.scrollContainer.nativeElement.scrollTop;
      const savedState = this.componentState.restoreState<SavedSchemaExplorerState>(this.getStateKey());
      if (savedState) {
        savedState.scrollPosition = scrollTop;
        this.componentState.saveState(this.getStateKey(), savedState);
      }
    }
  }

  private restoreScrollPosition(): void {
    if (this.scrollRestored || !this.scrollContainer?.nativeElement) return;
    
    const savedState = this.componentState.restoreState<SavedSchemaExplorerState>(this.getStateKey());
    if (savedState?.scrollPosition) {
      // Use setTimeout to ensure DOM is fully rendered
      setTimeout(() => {
        if (this.scrollContainer?.nativeElement) {
          this.scrollContainer.nativeElement.scrollTop = savedState.scrollPosition;
          this.scrollRestored = true;
        }
      }, 50);
    }
  }

  private restoreState(): void {
    if (!this.sessionId) return;
    
    const savedState = this.componentState.restoreState<SavedSchemaExplorerState>(this.getStateKey());
    if (savedState) {
      // Restore search term
      this.searchTerm = savedState.searchTerm || '';
      
      // Restore view mode
      this.viewMode = savedState.viewMode || 'list';
      
      // Restore schema data if available
      if (savedState.schemaData) {
        this.schemaData = savedState.schemaData;
        
        // Restore expanded state
        if (savedState.expandedTables) {
          this.schemaData!.tables.forEach(table => {
            table.expanded = savedState.expandedTables.includes(table.table_name);
          });
        }
        
        // Restore selected table
        if (savedState.selectedTableName) {
          const table = this.schemaData!.tables.find(t => t.table_name === savedState.selectedTableName);
          if (table) {
            this.selectedTable = table;
            if (table.ai_description) {
              this.tableDescription = table.ai_description;
            }
          }
        }
      }
      
      // Restore scroll position after a short delay to ensure DOM is ready
      if (savedState.scrollPosition && savedState.scrollPosition > 0) {
        setTimeout(() => {
          this.restoreScrollPosition();
        }, 200);
      }
    }
  }
}
