/* Phase 0 ships no behaviour: the shell is server-rendered and works with
 * JavaScript disabled. The file exists so Phase 1's entry-form logic has an
 * obvious home, and so the Content-Security-Policy's script-src 'self' is
 * exercised from the start rather than discovered to be wrong later.
 */
