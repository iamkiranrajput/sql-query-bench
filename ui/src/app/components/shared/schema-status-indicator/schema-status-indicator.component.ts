import { Component, Input, OnInit, OnDestroy, OnChanges, SimpleChanges } from '@angular/core';
import { CommonModule } from '@angular/common';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { ApiService } from '../../../services/api.service';
import { interval, Subscription } from 'rxjs';
import { switchMap, catchError } from 'rxjs/operators';
import { of } from 'rxjs';

interface SchemaStatus {
  success: boolean;
  status: 'ready' | 'initializing' | 'unavailable' | 'not_started';
  has_embeddings: boolean;
  tables_count?: number;
  columns_count?: number;
  message?: string;
  cached?: boolean;
}

@Component({
  selector: 'app-schema-status-indicator',
  standalone: true,
  imports: [CommonModule, MatIconModule, MatProgressSpinnerModule],
  template: `
    <div *ngIf="status === 'ready' || status === 'initializing'" class="flex items-center space-x-2" [class]="getStatusClasses()">
      <!-- Status Dot with Enhanced Visibility -->
      <div 
        class="w-3 h-3 rounded-full transition-all duration-300 flex-shrink-0"
        [class]="getDotClasses()"
        [title]="statusMessage"
      ></div>
      
      <!-- Status Text (optional, can be hidden) -->
      <span *ngIf="showText" class="text-xs font-medium" [class]="getTextClasses()">
        {{ statusText }}
      </span>
      
      <!-- Loading Spinner (when initializing) - More Visible -->
      <mat-spinner 
        *ngIf="status === 'initializing'" 
        diameter="14" 
        class="!mr-1"
        [style.color]="'#eab308'"
      ></mat-spinner>
    </div>
  `,
  styles: [`
    :host {
      display: inline-block;
    }
    
    /* Enhanced blinking animation for initializing state */
    .blink-slow {
      animation: blink-slow 2s ease-in-out infinite;
    }
    
    @keyframes blink-slow {
      0%, 100% {
        opacity: 1;
        transform: scale(1);
      }
      50% {
        opacity: 0.6;
        transform: scale(0.95);
      }
    }
    
    /* Pulse animation for initializing */
    .pulse-yellow {
      animation: pulse-yellow 2s cubic-bezier(0.4, 0, 0.6, 1) infinite;
    }
    
    @keyframes pulse-yellow {
      0%, 100% {
        opacity: 1;
        box-shadow: 0 0 0 0 rgba(234, 179, 8, 0.7);
      }
      50% {
        opacity: 0.8;
        box-shadow: 0 0 0 4px rgba(234, 179, 8, 0);
      }
    }
  `]
})
export class SchemaStatusIndicatorComponent implements OnInit, OnDestroy, OnChanges {
  @Input() sessionId: string | null = null;
  @Input() showText: boolean = false; // Show text label
  @Input() updateInterval: number = 5000; // Poll every 5 seconds (reduced frequency)

  status: SchemaStatus['status'] = 'not_started';
  statusMessage: string = 'Checking schema intelligence status...';
  statusText: string = '';
  has_embeddings: boolean = false;
  tablesCount?: number;
  columnsCount?: number;
  
  private statusSubscription?: Subscription;

  constructor(private apiService: ApiService) {}

  ngOnInit(): void {
    if (this.sessionId) {
      // CRITICAL FIX: Set initializing status immediately to show yellow dot
      // Don't wait for first API call - show loading state right away
      this.status = 'initializing';
      this.statusMessage = 'Checking schema intelligence status...';
      this.statusText = 'Initializing...';
      
      this.checkStatus();
      // Poll for status updates (stop if session invalid or status unavailable)
      // Use a small delay before starting polling to avoid immediate duplicate requests
      setTimeout(() => {
        if (!this.sessionId) return; // Check again after delay
        
        this.statusSubscription = interval(this.updateInterval)
          .pipe(
            switchMap(() => {
              // Stop polling if no session ID
              if (!this.sessionId) {
                this.stopPolling();
                return of(null);
              }
              // Stop polling if status is unavailable (session expired/invalid)
              if (this.status === 'unavailable' && !this.has_embeddings) {
                this.stopPolling();
                return of(null);
              }
              // Stop polling if ready (no need to keep checking)
              if (this.status === 'ready') {
                this.stopPolling();
                return of(null);
              }
              return this.apiService.getSchemaIntelligenceStatus(this.sessionId).pipe(
                catchError((error) => {
                  // Stop polling on error (likely session expired)
                  console.warn('Status check failed, stopping polling:', error);
                  this.status = 'unavailable';
                  this.statusMessage = 'Session expired or unavailable';
                  this.statusText = 'Unavailable';
                  this.stopPolling();
                  return of(null);
                })
              );
            })
          )
          .subscribe(response => {
            if (response?.success) {
              this.updateStatus(response);
              // Stop polling if status is unavailable and not initializing
              if (response.status === 'unavailable' && !response.has_embeddings) {
                this.stopPolling();
              }
              // Stop polling if ready (no need to keep checking)
              if (response.status === 'ready') {
                // Stop polling when ready - status won't change
                this.stopPolling();
              }
            } else if (response && !response.success) {
              // Stop polling if API explicitly says unavailable
              this.status = 'unavailable';
              this.statusMessage = response.message || 'Status unavailable';
              this.statusText = 'Unavailable';
              this.stopPolling();
            }
          });
      }, 1000); // 1 second delay before starting polling
    }
  }
  
  private stopPolling(): void {
    if (this.statusSubscription) {
      this.statusSubscription.unsubscribe();
      this.statusSubscription = undefined;
    }
  }

  ngOnDestroy(): void {
    this.stopPolling();
  }
  
  // Stop polling when sessionId changes to null
  ngOnChanges(changes: SimpleChanges): void {
    if (changes['sessionId']) {
      if (!this.sessionId && this.statusSubscription) {
        this.stopPolling();
        this.status = 'not_started';
        this.statusMessage = 'No active session';
        this.statusText = '';
      } else if (this.sessionId && !this.statusSubscription) {
        // CRITICAL FIX: Set initializing status immediately when sessionId is set
        this.status = 'initializing';
        this.statusMessage = 'Checking schema intelligence status...';
        this.statusText = 'Initializing...';
        // Restart polling if sessionId is set and we're not already polling
        this.ngOnInit();
      }
    }
  }

  checkStatus(): void {
    if (!this.sessionId) return;
    
    this.apiService.getSchemaIntelligenceStatus(this.sessionId).subscribe({
      next: (response) => {
        if (response?.success) {
          this.updateStatus(response);
        }
      },
      error: (error) => {
        console.error('Failed to check schema intelligence status:', error);
        this.status = 'unavailable';
        this.statusMessage = 'Unable to check status';
        this.statusText = 'Status unavailable';
      }
    });
  }

  private updateStatus(response: SchemaStatus): void {
    this.status = response.status;
    this.has_embeddings = response.has_embeddings;
    this.tablesCount = response.tables_count;
    this.columnsCount = response.columns_count;
    this.statusMessage = response.message || this.getDefaultMessage();
    this.statusText = this.getStatusText();
  }

  getStatusClasses(): string {
    switch (this.status) {
      case 'ready':
        return 'text-green-700';
      case 'initializing':
        return 'text-yellow-600';
      case 'unavailable':
        return 'text-gray-500';
      default:
        return 'text-gray-400';
    }
  }

  getDotClasses(): string {
    switch (this.status) {
      case 'ready':
        return 'bg-green-500 shadow-sm shadow-green-500/50';
      case 'initializing':
        // Enhanced yellow blinking dot - more visible and prominent
        return 'bg-yellow-500 shadow-lg shadow-yellow-500/60 pulse-yellow blink-slow';
      case 'unavailable':
        return 'bg-gray-400';
      default:
        // Show yellow/pulsing even for not_started if we're checking status
        return this.statusMessage.includes('Checking') || this.statusMessage.includes('initializing') 
          ? 'bg-yellow-500 shadow-lg shadow-yellow-500/60 pulse-yellow blink-slow'
          : 'bg-gray-300';
    }
  }

  getTextClasses(): string {
    return this.getStatusClasses();
  }

  getStatusText(): string {
    switch (this.status) {
      case 'ready':
        const counts = [];
        if (this.tablesCount) counts.push(`${this.tablesCount} tables`);
        if (this.columnsCount) counts.push(`${this.columnsCount} columns`);
        return counts.length > 0 ? `Ready (${counts.join(', ')})` : 'Ready';
      case 'initializing':
        return 'Initializing...';
      case 'unavailable':
        return 'Unavailable';
      default:
        return 'Not started';
    }
  }

  private getDefaultMessage(): string {
    switch (this.status) {
      case 'ready':
        return 'Schema intelligence is ready. You can now query with enhanced table discovery.';
      case 'initializing':
        return 'Schema intelligence is initializing in the background. This may take a few moments.';
      case 'unavailable':
        return 'Schema intelligence is not available for this database.';
      default:
        return 'Schema intelligence initialization has not started.';
    }
  }
}
