import { Pipe, PipeTransform } from '@angular/core';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';

/**
 * Converts simple markdown to HTML for chat messages.
 * Supports: **bold**, `code`, *italic*
 */
@Pipe({
  name: 'simpleMarkdown',
  standalone: true
})
export class SimpleMarkdownPipe implements PipeTransform {
  constructor(private sanitizer: DomSanitizer) {}

  transform(value: string): SafeHtml {
    if (!value) return '';

    let html = this.escapeHtml(value);

    // **bold** → <strong>bold</strong>
    html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');

    // `code` → <code>code</code>
    html = html.replace(/`([^`]+)`/g, '<code class="inline-code">$1</code>');

    // *italic* → <em>italic</em> (single asterisk, but not inside words)
    html = html.replace(/(?<!\w)\*([^*]+)\*(?!\w)/g, '<em>$1</em>');

    return this.sanitizer.bypassSecurityTrustHtml(html);
  }

  private escapeHtml(text: string): string {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }
}
