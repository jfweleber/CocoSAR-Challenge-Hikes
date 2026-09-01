/**
 * lightbox.js
 *
 * A small dependency-free photo lightbox / carousel. Replaces the old
 * "click a photo, it opens full-size in a new tab" behavior with an in-page
 * overlay that supports prev/next, keyboard arrows, Esc / click-outside to
 * close, and swipe on touch devices.
 *
 * Usage: wrap a set of thumbnails in a container marked data-lightbox, where
 * each thumbnail is an <a class="photo-thumb" href="<full-size>"> (optionally
 * with data-caption). Every such container on the page becomes its own
 * carousel group, so next/prev cycles within that completion's photos only.
 *
 * One overlay element is built and reused for every gallery on the page.
 */
(function () {
    var galleries = document.querySelectorAll('.photo-gallery[data-lightbox]');
    if (!galleries.length) return;

    // ---- Build the single shared overlay ----
    var overlay = document.createElement('div');
    overlay.className = 'lightbox';
    overlay.setAttribute('aria-hidden', 'true');
    overlay.innerHTML =
        '<button class="lightbox-btn lightbox-close" aria-label="Close">&times;</button>' +
        '<button class="lightbox-btn lightbox-prev" aria-label="Previous photo">&#10094;</button>' +
        '<figure class="lightbox-figure">' +
            '<img class="lightbox-img" alt="">' +
            '<figcaption class="lightbox-caption"></figcaption>' +
        '</figure>' +
        '<button class="lightbox-btn lightbox-next" aria-label="Next photo">&#10095;</button>';
    document.body.appendChild(overlay);

    var imgEl = overlay.querySelector('.lightbox-img');
    var capEl = overlay.querySelector('.lightbox-caption');
    var items = [];     // [{ src, caption }] for the currently-open gallery
    var index = 0;

    function show(i) {
        // Wrap around at both ends so next from the last photo loops to the first.
        index = (i + items.length) % items.length;
        var item = items[index];
        imgEl.src = item.src;
        imgEl.alt = item.caption || 'photo';
        capEl.textContent = item.caption || '';
        capEl.style.display = item.caption ? '' : 'none';
    }

    function open(groupItems, i) {
        items = groupItems;
        // Hide the prev/next chrome when there's only one photo to page through.
        overlay.classList.toggle('lightbox-single', items.length <= 1);
        overlay.classList.add('is-open');
        overlay.setAttribute('aria-hidden', 'false');
        document.body.classList.add('lightbox-lock');   // freeze background scroll
        show(i);
    }

    function close() {
        overlay.classList.remove('is-open');
        overlay.setAttribute('aria-hidden', 'true');
        document.body.classList.remove('lightbox-lock');
        imgEl.src = '';     // release the (possibly large) image from memory
    }

    // ---- Wire each gallery to the overlay ----
    galleries.forEach(function (gallery) {
        var links = Array.prototype.slice.call(gallery.querySelectorAll('a.photo-thumb'));
        var groupItems = links.map(function (a) {
            return { src: a.getAttribute('href'), caption: a.getAttribute('data-caption') };
        });
        links.forEach(function (a, i) {
            a.addEventListener('click', function (e) {
                e.preventDefault();         // don't navigate to the raw file
                open(groupItems, i);
            });
        });
    });

    // ---- Controls ----
    // stopPropagation on the arrows so their click doesn't bubble to the
    // overlay's click-to-close handler below.
    overlay.querySelector('.lightbox-next').addEventListener('click', function (e) { e.stopPropagation(); show(index + 1); });
    overlay.querySelector('.lightbox-prev').addEventListener('click', function (e) { e.stopPropagation(); show(index - 1); });
    overlay.querySelector('.lightbox-close').addEventListener('click', close);

    // Click on the backdrop (or the figure padding, but not the image) closes.
    overlay.addEventListener('click', function (e) {
        if (e.target === overlay || e.target.classList.contains('lightbox-figure')) close();
    });

    document.addEventListener('keydown', function (e) {
        if (!overlay.classList.contains('is-open')) return;
        if (e.key === 'Escape') close();
        else if (e.key === 'ArrowRight') show(index + 1);
        else if (e.key === 'ArrowLeft') show(index - 1);
    });

    // ---- Touch swipe (horizontal) ----
    var startX = null;
    overlay.addEventListener('touchstart', function (e) { startX = e.touches[0].clientX; }, { passive: true });
    overlay.addEventListener('touchend', function (e) {
        if (startX === null) return;
        var dx = e.changedTouches[0].clientX - startX;
        if (Math.abs(dx) > 40) show(index + (dx < 0 ? 1 : -1));   // swipe left = next
        startX = null;
    });
})();
