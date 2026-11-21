import { expect, it } from 'vitest'
import { applyRewriteCandidate } from '@/components/editor/rewriteApplication'
const source = { chapterId:'c', sourceText:'前文。选段。后文。', start:3, end:6, text:'选段。' }
it('inserts continuation after selection while retaining both sides', () => {
  expect(applyRewriteCandidate(source,'c',source.sourceText,'续写。','continue')).toBe('前文。选段。续写。后文。')
})
it('replaces only selected text for ordinary rewrite', () => {
  expect(applyRewriteCandidate(source,'c',source.sourceText,'修订。','custom')).toBe('前文。修订。后文。')
})
it('rejects changed chapter, changed surrounding facts, invalid selection and empty reply', () => {
  expect(applyRewriteCandidate(source,'other',source.sourceText,'新。','custom')).toBeNull()
  expect(applyRewriteCandidate(source,'c','新文。选段。后文。','新。','custom')).toBeNull()
  expect(applyRewriteCandidate({...source,end:7},'c',source.sourceText,'新。','custom')).toBeNull()
  expect(applyRewriteCandidate(source,'c',source.sourceText,'  ','custom')).toBeNull()
})
