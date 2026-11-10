import ImageListWidgetTemplate from '../templates/imageListWidget.pug';
import '../stylesheets/imageListWidget.styl';

const $ = girder.$;
const View = girder.views.View;

/** Splits "4.6.1, 4.6.0" or a pasted column of tags into separate tags. */
const splitTags = (text) => text.split(/[\s,]+/).map((tag) => tag.trim()).filter(Boolean);

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

        // A pasted list, or a tag typed and then tabbed away from, is still
        // meant to be added.
        'paste .g-sivacor-tag-input'(event) {
            const input = this.$(event.currentTarget);
            setTimeout(() => this._addTags(input), 0);
        },
        'blur .g-sivacor-tag-input'(event) {
            const input = this.$(event.currentTarget);
            if (input.val().trim()) {
                this._addTags(input, { refocus: false });
            }
        },

        'click .g-sivacor-tag-remove'(event) {
            event.preventDefault();
            const chip = this.$(event.currentTarget).closest('.g-sivacor-tag');
            const index = this._index(chip);
            this.rows[index].tags.splice(chip.index(), 1);
            this._renderRow(index);
            this.$(`tr[data-index=${index}] .g-sivacor-tag-input`).trigger('focus');
        },

        'click .g-sivacor-tag-default'(event) {
            event.preventDefault();
            const chip = this.$(event.currentTarget).closest('.g-sivacor-tag');
            const index = this._index(chip);
            const tags = this.rows[index].tags;
            tags.unshift(...tags.splice(chip.index(), 1));
            this._renderRow(index);
        },

        'input .g-sivacor-image-filter'(event) {
            this.filter = this.$(event.currentTarget).val().trim().toLowerCase();
            this._applyFilter();
        },

        'click .g-sivacor-toggle-json'(event) {
            event.preventDefault();
            if (this.jsonMode) {
                // Leaving JSON is the one switch that can fail; stay put if so.
                const parsed = this._parseJson();
                if (parsed.error) {
                    this.trigger('g:error', parsed.error);
                    return;
                }
                this.setValue(parsed.value);
            } else {
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
        const seen = new Set();
        for (const row of this.rows) {
            if (!row.image && !row.tags.length) {
                continue;
            }
            if (!row.image) {
                return { error: `Name the image that has the tags ${row.tags.join(', ')}.` };
            }
            if (seen.has(row.image)) {
                return { error: `${row.image} is listed twice. Merge its tags into one row.` };
            }
            if (!row.tags.length) {
                return { error: `${row.image} has no tags, so nothing on it could be used. Add a tag or remove the image.` };
            }
            seen.add(row.image);
        }
        return { value: this._toObject() };
    },

    render() {
        this.$el.html(ImageListWidgetTemplate({
            rows: this.rows,
            filter: this.filter,
            jsonMode: this.jsonMode,
            jsonText: this.jsonText
        }));
        if (!this.jsonMode) {
            this._applyFilter();
            this._renderSummary();
        }
        return this;
    },

    _addTags(input, { refocus = true } = {}) {
        const index = this._index(input);
        const tags = this.rows[index].tags;
        const fresh = splitTags(input.val()).filter((tag) => !tags.includes(tag));
        const repeated = splitTags(input.val()).filter((tag) => tags.includes(tag));
        input.val('');
        // Newest first, in the order they were typed or pasted.
        tags.unshift(...[...new Set(fresh)]);
        this._renderRow(index);
        const row = this.$(`tr[data-index=${index}]`);
        repeated.forEach((tag) => {
            row.find('.g-sivacor-tag').filter((i, el) => $(el).data('tag') === tag)
                .addClass('g-sivacor-tag-flash');
        });
        if (refocus) {
            row.find('.g-sivacor-tag-input').trigger('focus');
        }
    },

    /** Re-render one row in place, so typing elsewhere keeps its focus. */
    _renderRow(index) {
        // girder.$, not this.$: the view's own $ is find() within the view,
        // and would read the rendered HTML as a selector.
        const fresh = $(ImageListWidgetTemplate({
            rows: this.rows, filter: '', jsonMode: false, jsonText: ''
        })).find(`tr[data-index=${index}]`);
        this.$(`tr[data-index=${index}]`).replaceWith(fresh);
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

    _parseJson() {
        const text = this.$('.g-sivacor-image-json').val();
        let value;
        try {
            value = JSON.parse(text);
        } catch (err) {
            return { error: `The image list is not valid JSON: ${err.message}` };
        }
        if (!value || typeof value !== 'object' || Array.isArray(value)) {
            return { error: 'The image list must be a JSON object of image name to tags.' };
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
