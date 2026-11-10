import ImageListWidgetTemplate from '../templates/imageListWidget.pug';
import ImageTagTemplate from '../templates/imageTag.pug';
import '../stylesheets/imageListWidget.styl';

const $ = girder.$;
const View = girder.views.View;

/** Splits "4.6.1, 4.6.0" or a pasted column of tags into separate tags. */
const splitTags = (text) => text.split(/[\s,]+/).map((tag) => tag.trim()).filter(Boolean);

/** Read with attr(), never data(): jQuery's data() turns "2023" into 2023. */
const tagOf = (el) => $(el).attr('data-tag');

/**
 * What the server's ``image_tags.check_image_tags`` refuses, or ``null``.
 *
 * ``complete: false`` checks only that the value is a list the editor can
 * show, so switching modes never refuses an empty or unfinished list -- the
 * JSON view is where a whole list gets pasted in.
 */
const checkList = (value, { complete = true } = {}) => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
        return 'The image list must be a JSON object of image name to a list of tags.';
    }
    const entries = Object.entries(value);
    if (complete && !entries.length) {
        return 'Add at least one image. An empty list would refuse every submission.';
    }
    for (const [image, tags] of entries) {
        if (complete && !image.trim()) {
            return 'Every image needs a name.';
        }
        if (!Array.isArray(tags) || !tags.every((tag) => typeof tag === 'string' && tag.trim())) {
            return `Tags for ${image || 'the unnamed image'} must be a list of non-empty strings, e.g. ["4.6.1"].`;
        }
        if (complete && !tags.length) {
            return `${image} has no tags, so nothing on it could be used. Add a tag or remove the image.`;
        }
    }
    return null;
};

/**
 * An editor for ``sivacor.image_tags``: one row per image, its tags as chips.
 *
 * Each image's tags keep their order, because the submission form pre-selects
 * the *first* one. That is why the first chip is marked, why clicking a chip
 * moves it to the front, and why a new tag goes in front rather than at the
 * end -- the lists run newest first. Images themselves come back from the
 * server sorted by name, so their order is not something to edit.
 *
 * The raw JSON stays one click away, for pasting a whole list in at once.
 */
const ImageListWidget = View.extend({
    events: {
        'click .g-sivacor-add-image'(event) {
            event.preventDefault();
            this.rows.unshift({ image: '', tags: [] });
            this.filter = '';
            this.render();
            this.$('.g-sivacor-image-name').first().trigger('focus');
        },

        'input .g-sivacor-image-name'(event) {
            const input = this.$(event.currentTarget);
            this._row(input).image = input.val().trim();
            this._renderSummary();
        },

        // Enter would submit the surrounding form, saving a half-typed row.
        'keydown .g-sivacor-image-name, .g-sivacor-image-filter'(event) {
            if (event.key === 'Enter') {
                event.preventDefault();
                this.$(event.currentTarget).closest('tr').find('.g-sivacor-tag-input').trigger('focus');
            }
        },

        'click .g-sivacor-remove-image'(event) {
            event.preventDefault();
            this.rows.splice(this._index(this.$(event.currentTarget)), 1);
            this.render();
        },

        'keydown .g-sivacor-tag-input'(event) {
            if (event.key === 'Enter' || event.key === ',') {
                event.preventDefault();
                this._addTags(this.$(event.currentTarget));
            }
        },

        // A pasted list, or a tag typed and then clicked or tabbed away from,
        // is still meant to be added.

        'paste .g-sivacor-tag-input'(event) {
            const input = this.$(event.currentTarget);
            setTimeout(() => this._addTags(input), 0);
        },
        'blur .g-sivacor-tag-input'(event) {
            const input = this.$(event.currentTarget);
            if (input.val().trim()) {
                this._addTags(input, { refocus: false, defer: this._pointerDown });
            }
        },

        // mousedown runs before the blur it causes, so the blur can tell a
        // click from a Tab.
        mousedown() {
            this._pointerDown = true;
            $(document).one('mouseup', () => {
                this._pointerDown = false;
            });
        },

        'click .g-sivacor-tag-remove'(event) {
            event.preventDefault();
            const chip = this.$(event.currentTarget).closest('.g-sivacor-tag');
            const index = this._index(chip);
            this.rows[index].tags.splice(this.rows[index].tags.indexOf(tagOf(chip)), 1);
            this._syncChips(index);
            this.$(`tr[data-index=${index}] .g-sivacor-tag-input`).trigger('focus');
        },

        'click .g-sivacor-tag-default'(event) {
            event.preventDefault();
            const chip = this.$(event.currentTarget).closest('.g-sivacor-tag');
            const index = this._index(chip);
            const tags = this.rows[index].tags;
            tags.unshift(...tags.splice(tags.indexOf(tagOf(chip)), 1));
            this._syncChips(index);
        },

        'input .g-sivacor-image-filter'(event) {
            this.filter = this.$(event.currentTarget).val().trim().toLowerCase();
            this._applyFilter();
        },

        'click .g-sivacor-toggle-json'(event) {
            event.preventDefault();
            // Either switch refuses rather than lose something: JSON the rows
            // cannot show, or rows the JSON cannot hold.
            if (this.jsonMode) {
                const parsed = this._parseJson({ complete: false });
                if (parsed.error) {
                    this.trigger('g:error', parsed.error);
                    return;
                }
                this.setValue(parsed.value);
            } else {
                const lost = this._rowsJsonCannotHold();
                if (lost) {
                    this.trigger('g:error', lost);
                    return;
                }
                this.jsonText = JSON.stringify(this._toObject(), null, 2);
            }
            this.jsonMode = !this.jsonMode;
            this.trigger('g:error', '');
            this.render();
        }
    },

    initialize() {
        this.rows = [];
        this.filter = '';
        this.jsonMode = false;
        this.jsonText = '';
    },

    setValue(tags) {
        this.rows = Object.entries(tags || {}).map(([image, list]) => ({
            image,
            tags: Array.isArray(list) ? list.slice() : []
        }));
        this.jsonText = JSON.stringify(tags || {}, null, 2);
        return this.render();
    },

    /**
     * ``{value}`` ready to save, or ``{error}`` naming what to fix. Checks
     * here what the server would refuse less helpfully, plus the two it would
     * accept and should not: an image with no tags allows nothing, and a
     * duplicated image would silently lose one of its rows.
     */
    getValue() {
        if (this.jsonMode) {
            return this._parseJson();
        }
        const lost = this._rowsJsonCannotHold();
        if (lost) {
            return { error: lost };
        }
        const value = this._toObject();
        const error = checkList(value);
        return error ? { error } : { value };
    },

    /**
     * A row with tags but no name, or two rows for one image. Neither survives
     * conversion to an object -- the first is dropped, the second collapses to
     * one row -- so both are refused rather than quietly lost.
     */
    _rowsJsonCannotHold() {
        const seen = new Set();
        for (const row of this.rows) {
            if (!row.image) {
                if (row.tags.length) {
                    return `Name the image that has the tags ${row.tags.join(', ')}.`;
                }
                continue;
            }
            if (seen.has(row.image)) {
                return `${row.image} is listed twice. Merge its tags into one row.`;
            }
            seen.add(row.image);
        }
        return null;
    },

    render() {
        this.$el.html(ImageListWidgetTemplate({
            rows: this.rows,
            filter: this.filter,
            jsonMode: this.jsonMode,
            jsonText: this.jsonText,
            tagTemplate: ImageTagTemplate
        }));
        if (!this.jsonMode) {
            this._applyFilter();
            this._renderSummary();
        }
        return this;
    },

    /**
     * Add what is typed in ``input`` to its row.
     *
     * ``defer`` is for a blur caused by a click: the tag joins the row's data
     * at once, but its chip waits until the pointer is released. Drawn now, it
     * would push every chip after it along, the mouseup would land on a
     * different element than the mousedown, and the browser would fire no
     * click at all -- on the chip or the trash icon the user was aiming at.
     * The click's own handler then acts on data that already holds the tag.
     */
    _addTags(input, { refocus = true, defer = false } = {}) {
        const index = this._index(input);
        const owner = this.rows[index];
        const tags = owner.tags;
        const fresh = splitTags(input.val()).filter((tag) => !tags.includes(tag));
        const repeated = splitTags(input.val()).filter((tag) => tags.includes(tag));
        input.val('');
        // Newest first, in the order they were typed or pasted.
        tags.unshift(...[...new Set(fresh)]);
        if (defer) {
            $(document).one('mouseup', () => setTimeout(() => {
                // By row, not index: the click may have removed a row above.
                const now = this.rows.indexOf(owner);
                if (now >= 0 && !this.jsonMode) {
                    this._syncChips(now);
                }
            }, 0));
            return;
        }
        this._syncChips(index);
        const row = this.$(`tr[data-index=${index}]`);
        row.find('.g-sivacor-tag').filter((i, el) => repeated.includes(tagOf(el)))
            .removeClass('g-sivacor-tag-flash')
            .each((i, el) => el.offsetWidth) // restart the animation on a repeat
            .addClass('g-sivacor-tag-flash');
        if (refocus) {
            row.find('.g-sivacor-tag-input').trigger('focus');
        }
    },

    /**
     * Bring one row's chips in line with its tags, keeping every chip element
     * that is still wanted.
     *
     * Not a re-render, and that is the point: adding a typed tag happens on
     * the input's blur, which fires on mousedown -- before the click it was
     * caused by. Replacing the row there would detach the trash icon or chip
     * under the pointer, and the click would land on nothing.
     */
    _syncChips(index) {
        const list = this.$(`tr[data-index=${index}] .g-sivacor-tags`);
        const entry = list.children('.g-sivacor-tag-entry');
        const existing = new Map();
        list.children('.g-sivacor-tag').each((i, el) => existing.set(tagOf(el), el));
        const wanted = new Set(this.rows[index].tags);
        existing.forEach((el, tag) => {
            if (!wanted.has(tag)) {
                $(el).remove();
                existing.delete(tag);
            }
        });
        // Walk the wanted order with a cursor, moving a chip only when it is
        // out of place, so chips that did not change stay where they are.
        let cursor = list.children('.g-sivacor-tag').get(0) || entry.get(0);
        this.rows[index].tags.forEach((tag, position) => {
            const known = existing.get(tag);
            existing.delete(tag);
            // girder.$, not this.$: the view's $ is find() within the view,
            // and would read the rendered HTML as a selector.
            const chip = known ? $(known) : $(ImageTagTemplate({ tag, first: false }));
            if (chip.get(0) === cursor) {
                cursor = cursor.nextElementSibling;
            } else {
                chip.insertBefore(cursor);
            }
            const first = position === 0;
            chip.toggleClass('g-sivacor-tag-first', first);
            chip.find('.g-sivacor-tag-default').prop('disabled', first).attr('title',
                first ? 'Pre-selected on the submission form' : 'Make this the pre-selected tag');
        });
        // A duplicate tag in the data (possible from JSON) gets one chip each;
        // any element left over is a second copy that is no longer wanted.
        existing.forEach((el) => $(el).remove());
        list.find('.g-sivacor-tag-input').attr('placeholder',
            this.rows[index].tags.length ? 'Add tag' : 'Add tags, e.g. 4.6.1, 4.6.0');
        this._applyFilter();
        this._renderSummary();
    },

    _applyFilter() {
        let shown = 0;
        this.$('tr[data-index]').each((i, el) => {
            const row = this.rows[$(el).data('index')];
            const match = !this.filter || !row.image ||
                row.image.toLowerCase().includes(this.filter) ||
                row.tags.some((tag) => tag.toLowerCase().includes(this.filter));
            $(el).toggle(match);
            shown += match ? 1 : 0;
        });
        this.$('.g-sivacor-filter-empty').toggle(!!this.filter && shown === 0);
    },

    _renderSummary() {
        const images = this.rows.filter((row) => row.image).length;
        const tags = this.rows.reduce((sum, row) => sum + row.tags.length, 0);
        this.$('.g-sivacor-image-count').text(
            `${images} ${images === 1 ? 'image' : 'images'}, ${tags} ${tags === 1 ? 'tag' : 'tags'}`
        );
    },

    _parseJson({ complete = true } = {}) {
        const text = this.$('.g-sivacor-image-json').val();
        let value;
        try {
            value = JSON.parse(text);
        } catch (err) {
            return { error: `The image list is not valid JSON: ${err.message}` };
        }
        const error = checkList(value, { complete });
        if (error) {
            return { error };
        }
        this.jsonText = text;
        return { value };
    },

    _toObject() {
        const out = {};
        this.rows.filter((row) => row.image).forEach((row) => {
            out[row.image] = row.tags.slice();
        });
        return out;
    },

    _index($el) {
        return Number($el.closest('tr').data('index'));
    },

    _row($el) {
        return this.rows[this._index($el)];
    }
});

export default ImageListWidget;
