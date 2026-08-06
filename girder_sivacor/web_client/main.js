import './stylesheets/telemetry.styl';

// Girder 5 loads each plugin bundle dynamically and exposes the core symbols on
// a `girder` global rather than as an importable module -- see
// girder/web/dist/src/src/pluginUtils.js. Destructuring at module scope is what
// the built-in jobs plugin does; these are deliberately not `import`ed.
const View = girder.views.View;
const router = girder.router;
const events = girder.events;
const { restRequest, cancelRestRequests } = girder.rest;
const { wrap } = girder.utilities.PluginUtils;
const AdminView = girder.views.body.AdminView;

const PAGE_SIZE = 50;

/** Every interpolated value goes through this. */
function escapeHtml(value) {
    if (value === null || value === undefined || value === '') {
        return '&mdash;';
    }
    return String(value).replace(/[&<>"']/g, (char) => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
    }[char]));
}

function formatDuration(seconds) {
    if (seconds === null || seconds === undefined) {
        return '&mdash;';
    }
    if (seconds < 90) {
        return `${Math.round(seconds)}s`;
    }
    if (seconds < 3600) {
        return `${(seconds / 60).toFixed(1)}m`;
    }
    return `${(seconds / 3600).toFixed(1)}h`;
}

function formatBytes(bytes) {
    if (!bytes) {
        return '&mdash;';
    }
    // Workspaces run from a few MiB to several GiB, so a fixed unit renders
    // most of them as either "0.0 GiB" or a wall of digits.
    if (bytes < 1024 ** 2) {
        return `${Math.round(bytes / 1024)} KiB`;
    }
    if (bytes < 1024 ** 3) {
        return `${Math.round(bytes / 1024 ** 2)} MiB`;
    }
    return `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
}

/** Turn the aggregation's [{_id, count}] rows into a lookup. */
function countsById(rows) {
    const counts = {};
    (rows || []).forEach((row) => {
        counts[row._id] = row.count;
    });
    return counts;
}

function statTile(label, value, modifier) {
    return `
        <div class="g-sivacor-tile ${modifier || ''}">
            <div class="g-sivacor-tile-value">${value}</div>
            <div class="g-sivacor-tile-label">${escapeHtml(label)}</div>
        </div>`;
}

function breakdownTable(title, emptyText, rows, columns) {
    if (!rows || !rows.length) {
        return `
            <div class="g-sivacor-panel">
                <h4>${escapeHtml(title)}</h4>
                <p class="g-sivacor-empty">${escapeHtml(emptyText)}</p>
            </div>`;
    }
    const head = columns.map((column) => `<th>${escapeHtml(column.label)}</th>`).join('');
    const body = rows
        .map((row) => `<tr>${columns.map((column) => `<td>${column.cell(row)}</td>`).join('')}</tr>`)
        .join('');
    return `
        <div class="g-sivacor-panel">
            <h4>${escapeHtml(title)}</h4>
            <table class="g-sivacor-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>
        </div>`;
}

const ExecutionRecordsView = View.extend({
    events: {
        'submit .g-sivacor-filter-form': function (event) {
            event.preventDefault();
            this.filters = {
                status: this.$('[name=status]').val(),
                errorCode: this.$('[name=errorCode]').val().trim(),
                imageName: this.$('[name=imageName]').val().trim(),
                since: this.$('[name=since]').val(),
                until: this.$('[name=until]').val()
            };
            // A new filter invalidates the page offset -- staying on page 4 of a
            // result set that now has one page shows an empty table, which reads
            // as "the filter matched nothing".
            this.offset = 0;
            this.fetch();
        },
        'click .g-sivacor-reset': function (event) {
            event.preventDefault();
            this.filters = {};
            this.offset = 0;
            this.fetch();
        },
        'click .g-sivacor-prev': function (event) {
            event.preventDefault();
            this.offset = Math.max(0, this.offset - PAGE_SIZE);
            this.fetch();
        },
        'click .g-sivacor-next': function (event) {
            event.preventDefault();
            if (this.offset + PAGE_SIZE < this.total) {
                this.offset += PAGE_SIZE;
                this.fetch();
            }
        },
        // Clicking a breakdown row filters by it: the question after "which
        // image fails most" is always "show me those runs".
        'click .g-sivacor-drill': function (event) {
            event.preventDefault();
            const target = this.$(event.currentTarget);
            this.filters = Object.assign({}, this.filters, {
                [target.data('field')]: String(target.data('value'))
            });
            this.offset = 0;
            this.fetch();
        }
    },

    initialize: function () {
        // Convention for a top-level Girder view: drop whatever the page we are
        // replacing still has in flight, so its response cannot land here and
        // render over us.
        cancelRestRequests('fetch');
        this.filters = {};
        this.offset = 0;
        this.total = 0;
        this.summary = null;
        this.records = null;
        this.error = null;
        this.loading = true;
        this.render();
        this.fetch();
    },

    activeFilters: function () {
        const params = {};
        Object.keys(this.filters).forEach((key) => {
            if (this.filters[key]) {
                params[key] = this.filters[key];
            }
        });
        return params;
    },

    fetch: function () {
        const params = this.activeFilters();
        this.loading = true;
        this.error = null;
        this.render();

        Promise.all([
            restRequest({ url: 'sivacor/execution_record/summary', data: params }),
            restRequest({
                url: 'sivacor/execution_record',
                data: Object.assign({ limit: PAGE_SIZE, offset: this.offset }, params)
            })
        ]).then(([summary, listing]) => {
            this.summary = summary;
            this.records = listing.records;
            this.total = listing.count;
            this.loading = false;
            this.render();
            return null;
        }).catch((resp) => {
            // Girder's global error banner also fires, but it scrolls away --
            // the page itself has to say why it is empty.
            this.error = (resp && resp.responseJSON && resp.responseJSON.message) ||
                'Could not load execution records.';
            this.loading = false;
            this.render();
        });
    },

    summaryHtml: function () {
        const summary = this.summary;
        const byStatus = countsById(summary.byStatus);
        const tiles = [
            statTile('Runs', summary.total),
            statTile('Completed', byStatus.completed || 0, 'g-sivacor-ok'),
            statTile('Failed', byStatus.failed || 0, 'g-sivacor-bad'),
            statTile('Reaped', byStatus.reaped || 0, 'g-sivacor-bad'),
            statTile('Mean duration', formatDuration(summary.duration.avg)),
            statTile('Longest', formatDuration(summary.duration.max))
        ].join('');

        const drill = (field, value, text) =>
            `<a href="#" class="g-sivacor-drill" data-field="${escapeHtml(field)}" ` +
            `data-value="${escapeHtml(value)}">${escapeHtml(text)}</a>`;

        const failures = breakdownTable(
            'Why runs failed', 'No failures in this selection.', summary.byErrorCode,
            [
                { label: 'Reason', cell: (row) => drill('errorCode', row._id, row._id) },
                { label: 'Runs', cell: (row) => escapeHtml(row.count) }
            ]
        );

        const images = breakdownTable(
            'By analysis image', 'No runs in this selection.', summary.byImage,
            [
                {
                    label: 'Image',
                    cell: (row) => drill('imageName', String(row._id).split(':')[0], row._id)
                },
                { label: 'Stages', cell: (row) => escapeHtml(row.count) },
                {
                    label: 'Did not complete',
                    cell: (row) => (row.failed
                        ? `<span class="g-sivacor-bad-text">${escapeHtml(row.failed)}</span>`
                        : '0')
                }
            ]
        );

        // Reverse-chronological: recent days are the ones being looked at.
        const dates = breakdownTable(
            'By day', 'No runs in this selection.',
            (summary.byDate || []).slice().reverse(),
            [
                { label: 'Date', cell: (row) => escapeHtml(row._id) },
                { label: 'Runs', cell: (row) => escapeHtml(row.count) },
                {
                    label: 'Did not complete',
                    cell: (row) => (row.failed
                        ? `<span class="g-sivacor-bad-text">${escapeHtml(row.failed)}</span>`
                        : '0')
                }
            ]
        );

        return `
            <div class="g-sivacor-tiles">${tiles}</div>
            <div class="g-sivacor-panels">${failures}${images}${dates}</div>`;
    },

    recordsHtml: function () {
        if (!this.records.length) {
            return '<p class="g-sivacor-empty">No execution records match this selection.</p>';
        }
        const rows = this.records.map((record) => {
            const stages = (record.stages || [])
                .map((stage) => escapeHtml(`${stage.image_name}:${stage.image_tag}`))
                .join('<br>') || '&mdash;';
            const peakOf = (field) => (record.stages || [])
                .map((stage) => stage[field])
                .filter((value) => value)
                .reduce((a, b) => Math.max(a, b), 0);
            const peakMemory = peakOf('max_memory_bytes');
            const peakDisk = peakOf('max_disk_bytes');
            const imageSize = peakOf('image_size_bytes');
            const error = record.error
                ? `${escapeHtml(record.error.code)}` +
                  (record.error.detail ? ` <code>${escapeHtml(record.error.detail)}</code>` : '') +
                  (record.error.step ? `<br><small>in ${escapeHtml(record.error.step)}</small>` : '')
                : '&mdash;';
            return `
                <tr>
                    <td>${escapeHtml(record.date)}</td>
                    <td><span class="g-sivacor-status g-sivacor-status-${escapeHtml(record.status)}">${escapeHtml(record.status)}</span></td>
                    <td>${stages}</td>
                    <td>${formatDuration(record.total_duration_seconds)}</td>
                    <td>${formatBytes(peakMemory)}</td>
                    <td>${formatBytes(peakDisk)}</td>
                    <td>${formatBytes(imageSize)}</td>
                    <td>${escapeHtml(record.package_size_bucket)}</td>
                    <td>${error}</td>
                    <td>${escapeHtml(record.stack_version)}</td>
                </tr>`;
        }).join('');

        const from = this.total ? this.offset + 1 : 0;
        const to = Math.min(this.offset + PAGE_SIZE, this.total);
        const prevDisabled = this.offset === 0 ? 'disabled' : '';
        const nextDisabled = this.offset + PAGE_SIZE >= this.total ? 'disabled' : '';

        return `
            <table class="g-sivacor-table g-sivacor-records">
                <thead><tr>
                    <th>Date</th><th>Status</th><th>Image</th><th>Duration</th>
                    <th>Peak memory</th><th>Peak disk</th><th>Image size</th>
                    <th>Package</th><th>Failure</th><th>Stack</th>
                </tr></thead>
                <tbody>${rows}</tbody>
            </table>
            <div class="g-sivacor-paging">
                <button class="g-sivacor-prev btn btn-sm btn-default" ${prevDisabled}>&laquo; Newer</button>
                <span>${from}&ndash;${to} of ${this.total}</span>
                <button class="g-sivacor-next btn btn-sm btn-default" ${nextDisabled}>Older &raquo;</button>
            </div>`;
    },

    filtersHtml: function () {
        const value = (key) => (this.filters[key] ? escapeHtml(this.filters[key]) : '');
        const selected = (key, option) => (this.filters[key] === option ? ' selected' : '');
        return `
            <form class="g-sivacor-filter-form form-inline">
                <select name="status" class="form-control input-sm">
                    <option value="">Any outcome</option>
                    <option value="completed"${selected('status', 'completed')}>Completed</option>
                    <option value="failed"${selected('status', 'failed')}>Failed</option>
                    <option value="reaped"${selected('status', 'reaped')}>Reaped</option>
                </select>
                <input name="errorCode" class="form-control input-sm" placeholder="Failure reason"
                       value="${value('errorCode')}">
                <input name="imageName" class="form-control input-sm" placeholder="Image name"
                       value="${value('imageName')}">
                <input name="since" type="date" class="form-control input-sm" value="${value('since')}">
                <input name="until" type="date" class="form-control input-sm" value="${value('until')}">
                <button type="submit" class="btn btn-sm btn-primary">Apply</button>
                <button class="g-sivacor-reset btn btn-sm btn-link">Reset</button>
            </form>`;
    },

    render: function () {
        let body;
        if (this.error) {
            body = `<div class="alert alert-danger">${escapeHtml(this.error)}</div>`;
        } else if (this.loading && !this.summary) {
            body = '<p class="g-sivacor-empty">Loading&hellip;</p>';
        } else if (this.summary) {
            body = this.summaryHtml() + this.recordsHtml();
        } else {
            body = '';
        }

        this.$el.html(`
            <div class="g-sivacor-telemetry${this.loading ? ' g-sivacor-loading' : ''}">
                <h3>Execution telemetry</h3>
                <p class="g-sivacor-lede">
                    Anonymous records of how submissions ran. These outlive the
                    submissions themselves &mdash; they are what remains after a
                    user deletes their data or the retention sweep does. They
                    hold no identifier of any kind, so a run cannot be traced
                    back to a person or to a submission from here.
                </p>
                ${this.filtersHtml()}
                ${body}
            </div>`);
        return this;
    }
});

router.route('sivacor/telemetry', 'sivacorTelemetry', function () {
    events.trigger('g:navigateTo', ExecutionRecordsView);
});

// The same hook the built-in jobs plugin uses to put "Jobs" on the admin console.
wrap(AdminView, 'render', function (render) {
    render.call(this);
    this.$('ul.g-admin-options').append(
        '<li class="g-sivacor-telemetry-link">' +
        '<a href="#sivacor/telemetry"><i class="icon-chart-bar"></i> Execution telemetry</a>' +
        '</li>'
    );
    return this;
});

girder.pluginUtils.registerPluginNamespace('sivacor', {
    views: { ExecutionRecordsView }
});
