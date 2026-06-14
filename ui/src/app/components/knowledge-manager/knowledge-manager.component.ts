/**
 * Governed Knowledge Manager (Bring-Your-Own-Governance)
 *
 * Lets a user add / edit / delete the governed business definitions that
 * Microsoft Foundry IQ grounds the SQL agent in — per `domain`, so one
 * knowledge index can serve many databases. Talks to the backend
 * `/api/knowledge` routes (Azure AI Search behind the scenes).
 *
 * Self-contained standalone component (own HttpClient calls) so it slots into
 * the app without touching shared services.
 */

import { CommonModule } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { Component, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { MatIconModule } from '@angular/material/icon';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { environment } from '../../../environments/environment';

interface GovernedDoc {
  id: string;
  title: string;
  content: string;
  source: string;
  category: string;
  domain: string;
}

interface ListResponse {
  configured: boolean;
  documents: GovernedDoc[];
  error: string | null;
}

interface WriteResponse {
  success: boolean;
  configured: boolean;
  document?: GovernedDoc;
  error: string | null;
}

@Component({
  selector: 'app-knowledge-manager',
  standalone: true,
  imports: [CommonModule, FormsModule, MatIconModule, MatProgressSpinnerModule],
  templateUrl: './knowledge-manager.component.html',
  styleUrls: ['./knowledge-manager.component.scss'],
})
export class KnowledgeManagerComponent implements OnInit {
  private readonly apiUrl = environment.apiUrl;

  configured = true;
  loading = false;
  saving = false;
  error: string | null = null;
  notice: string | null = null;

  documents: GovernedDoc[] = [];
  domainFilter = '';

  // Edit / create form state
  editing = false;
  form: GovernedDoc = this.blankForm();

  constructor(private http: HttpClient) {}

  ngOnInit(): void {
    this.refresh();
  }

  private blankForm(): GovernedDoc {
    return {
      id: '',
      title: '',
      content: '',
      source: '',
      category: 'business-glossary',
      domain: '',
    };
  }

  /** Distinct domain tags present, for the filter chips. */
  get domains(): string[] {
    const set = new Set<string>();
    for (const d of this.documents) {
      if (d.domain) {
        set.add(d.domain);
      }
    }
    return Array.from(set).sort();
  }

  get visibleDocuments(): GovernedDoc[] {
    if (!this.domainFilter) {
      return this.documents;
    }
    return this.documents.filter((d) => d.domain === this.domainFilter);
  }

  refresh(): void {
    this.loading = true;
    this.error = null;
    this.http
      .get<ListResponse>(`${this.apiUrl}/api/knowledge`)
      .subscribe({
        next: (res) => {
          this.configured = res.configured;
          this.documents = res.documents || [];
          this.error = res.error;
          this.loading = false;
        },
        error: (err) => {
          this.error = err?.error?.detail || err?.message || 'Failed to load knowledge.';
          this.loading = false;
        },
      });
  }

  startCreate(): void {
    this.form = this.blankForm();
    // Pre-fill the domain from the active filter for fast multi-add.
    this.form.domain = this.domainFilter || '';
    this.editing = true;
    this.notice = null;
  }

  startEdit(doc: GovernedDoc): void {
    this.form = { ...doc };
    this.editing = true;
    this.notice = null;
  }

  cancelEdit(): void {
    this.editing = false;
    this.form = this.blankForm();
  }

  save(): void {
    if (!this.form.title.trim() || !this.form.content.trim()) {
      this.error = 'Title and definition are required.';
      return;
    }
    this.saving = true;
    this.error = null;
    const body = {
      id: this.form.id || null,
      title: this.form.title,
      content: this.form.content,
      source: this.form.source,
      category: this.form.category,
      domain: this.form.domain,
    };
    this.http
      .post<WriteResponse>(`${this.apiUrl}/api/knowledge`, body)
      .subscribe({
        next: (res) => {
          this.saving = false;
          if (!res.success) {
            this.error = res.error || 'Save failed.';
            return;
          }
          this.notice = `Saved "${res.document?.title}". The agent can ground on it now.`;
          this.editing = false;
          this.form = this.blankForm();
          // Azure Search indexing is eventually consistent; refresh shortly.
          setTimeout(() => this.refresh(), 1200);
        },
        error: (err) => {
          this.saving = false;
          this.error = err?.error?.detail || err?.message || 'Save failed.';
        },
      });
  }

  remove(doc: GovernedDoc): void {
    if (!confirm(`Delete governed term "${doc.title}"?`)) {
      return;
    }
    this.error = null;
    this.http
      .delete<WriteResponse>(`${this.apiUrl}/api/knowledge/${encodeURIComponent(doc.id)}`)
      .subscribe({
        next: (res) => {
          if (!res.success) {
            this.error = res.error || 'Delete failed.';
            return;
          }
          this.notice = `Deleted "${doc.title}".`;
          setTimeout(() => this.refresh(), 1200);
        },
        error: (err) => {
          this.error = err?.error?.detail || err?.message || 'Delete failed.';
        },
      });
  }

  trackById(_i: number, doc: GovernedDoc): string {
    return doc.id;
  }
}
