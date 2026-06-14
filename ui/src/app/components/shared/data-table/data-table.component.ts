/**
 * Shared Data Table Component - Standard table design for all views
 * Optimized with OnPush change detection and trackBy
 */

import { Component, Input, Output, EventEmitter, OnChanges, SimpleChanges, ChangeDetectionStrategy, TrackByFunction } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';

export interface TableColumn {
  key: string;
  label: string;
  type?: 'text' | 'number' | 'date' | 'status' | 'actions';
  width?: string;
  align?: 'left' | 'center' | 'right';
  format?: (value: any, row?: any) => string;
  statusConfig?: {
    successValue: string;
    successIcon?: string;
    failureIcon?: string;
  };
  actionIcon?: string;
  actionTooltip?: string;
  actionVisible?: (row: any) => boolean;
}

export interface PaginationConfig {
  enabled: boolean;
  pageSize: number;
  pageSizeOptions: number[];
  totalCount: number;
  currentPage: number;
}

@Component({
  selector: 'app-data-table',
  standalone: true,
  imports: [CommonModule, FormsModule, MatIconModule, MatProgressSpinnerModule],
  templateUrl: './data-table.component.html',
  styleUrls: ['./data-table.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class DataTableComponent implements OnChanges {
  @Input() data: any[] = [];
  @Input() columns: TableColumn[] = [];
  @Input() loading = false;
  @Input() error: string | null = null;
  @Input() emptyMessage = 'No data available';
  @Input() emptyIcon = 'inbox';
  @Input() showDownload = false;
  @Input() pagination: PaginationConfig | null = null;
  @Input() rowClickable = false;
  @Input() title = '';
  @Input() subtitle = '';

  @Output() rowClick = new EventEmitter<any>();
  @Output() actionClick = new EventEmitter<{ row: any; columnKey: string }>();
  @Output() download = new EventEmitter<void>();
  @Output() pageChange = new EventEmitter<{ page: number; pageSize: number }>();
  @Output() retry = new EventEmitter<void>();

  displayColumns: string[] = [];

  // TrackBy function for better performance
  trackByIndex: TrackByFunction<any> = (index: number, item: any) => {
    return item.id || index;
  };

  trackByColumn: TrackByFunction<TableColumn> = (index: number, column: TableColumn) => {
    return column.key;
  };

  ngOnChanges(changes: SimpleChanges): void {
    if (changes['columns']) {
      this.displayColumns = this.columns.map(c => c.key);
    }
  }

  onRowClick(row: any): void {
    if (this.rowClickable) {
      this.rowClick.emit(row);
    }
  }

  onActionClick(event: Event, row: any, column: TableColumn): void {
    event.stopPropagation();
    this.actionClick.emit({ row, columnKey: column.key });
  }

  onDownload(): void {
    this.download.emit();
  }

  onRetry(): void {
    this.retry.emit();
  }

  // Pagination methods
  get totalPages(): number {
    if (!this.pagination) return 1;
    return Math.ceil(this.pagination.totalCount / this.pagination.pageSize);
  }

  get startRow(): number {
    if (!this.pagination) return 1;
    return (this.pagination.currentPage - 1) * this.pagination.pageSize + 1;
  }

  get endRow(): number {
    if (!this.pagination) return this.data.length;
    const end = this.pagination.currentPage * this.pagination.pageSize;
    return Math.min(end, this.pagination.totalCount);
  }

  firstPage(): void {
    if (this.pagination && this.pagination.currentPage > 1) {
      this.pageChange.emit({ page: 1, pageSize: this.pagination.pageSize });
    }
  }

  previousPage(): void {
    if (this.pagination && this.pagination.currentPage > 1) {
      this.pageChange.emit({ page: this.pagination.currentPage - 1, pageSize: this.pagination.pageSize });
    }
  }

  nextPage(): void {
    if (this.pagination && this.pagination.currentPage < this.totalPages) {
      this.pageChange.emit({ page: this.pagination.currentPage + 1, pageSize: this.pagination.pageSize });
    }
  }

  lastPage(): void {
    if (this.pagination && this.pagination.currentPage < this.totalPages) {
      this.pageChange.emit({ page: this.totalPages, pageSize: this.pagination.pageSize });
    }
  }

  onPageSizeChange(newSize: number): void {
    if (this.pagination) {
      this.pageChange.emit({ page: 1, pageSize: newSize });
    }
  }

  // Value formatting
  formatCellValue(column: TableColumn, row: any): string {
    const value = row[column.key];
    
    if (column.format) {
      return column.format(value, row);
    }

    if (value === null || value === undefined) {
      return '-';
    }

    if (column.type === 'date' && value) {
      return this.formatDate(value);
    }

    if (column.type === 'number' && typeof value === 'number') {
      return value.toLocaleString();
    }

    return String(value);
  }

  formatDate(value: string): string {
    try {
      const date = new Date(value);
      return date.toLocaleDateString('en-US', {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit'
      });
    } catch {
      return value;
    }
  }

  isSuccess(column: TableColumn, row: any): boolean {
    if (column.type === 'status' && column.statusConfig) {
      return row[column.key] === column.statusConfig.successValue;
    }
    return false;
  }

  getStatusIcon(column: TableColumn, row: any): string {
    if (column.type === 'status' && column.statusConfig) {
      const isSuccess = row[column.key] === column.statusConfig.successValue;
      return isSuccess 
        ? (column.statusConfig.successIcon || 'check_circle') 
        : (column.statusConfig.failureIcon || 'cancel');
    }
    return 'help';
  }

  getColumnAlign(column: TableColumn): string {
    if (column.align) return column.align;
    if (column.type === 'number') return 'right';
    if (column.type === 'status' || column.type === 'actions') return 'center';
    return 'left';
  }
}
