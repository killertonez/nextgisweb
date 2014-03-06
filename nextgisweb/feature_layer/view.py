# -*- coding: utf-8 -*-
import json
from types import MethodType

from pyramid.response import Response
from pyramid.renderers import render_to_response

from ..resource import Resource, resource_factory, Widget
from ..geometry import geom_from_wkt
from ..object_widget import ObjectWidget, CompositeWidget
from .. import dynmenu as dm

from .interface import IFeatureLayer
from .extension import FeatureExtension


class FeatureLayerFieldsWidget(Widget):
    interface = IFeatureLayer
    operation = ('update', )
    amdmod = 'ngw-feature-layer/FieldsWidget'


def feature_browse(layer, request):
    # TODO: Security
    return dict(obj=layer, subtitle=u"Объекты",
                maxwidth=True, maxheight=True)


def feature_show(layer, request):
    # TODO: Security

    fquery = layer.feature_query()
    fquery.filter_by(id=request.matchdict['feature_id'])

    feature = fquery().one()

    return dict(
        obj=layer,
        subtitle=u"Объект #%d" % feature.id,
        feature=feature)


def feature_edit(layer, request):
    # TODO: Security

    query = layer.feature_query()
    query.filter_by(id=request.matchdict['feature_id'])
    feature = list(query())[0]

    swconfig = []

    if hasattr(layer, 'feature_widget'):
        swconfig.append(('feature_layer', layer.feature_widget()))

    for k, v in FeatureExtension.registry._dict.iteritems():
        swconfig.append((k, v(layer).feature_widget))

    class Widget(CompositeWidget):
        subwidget_config = swconfig

    widget = Widget(obj=feature, operation='edit')
    widget.bind(request=request)

    if request.method == 'POST':
        widget.bind(data=request.json_body)

        if widget.validate():
            widget.populate_obj()

            return render_to_response(
                'json', dict(
                    status_code=200,
                    redirect=request.url
                ),
                request
            )

        else:
            return render_to_response(
                'json', dict(
                    status_code=400,
                    error=widget.widget_error()
                ),
                request
            )

    return dict(
        widget=widget,
        obj=layer,
        subtitle=u"Объект: %s" % unicode(feature),
    )


def field_collection(layer, request):
    # TODO: Security
    return [f.to_dict() for f in layer.fields]


def store_collection(layer, request):
    # TODO: Security

    query = layer.feature_query()

    http_range = request.headers.get('range', None)
    if http_range and http_range.startswith('items='):
        first, last = map(int, http_range[len('items='):].split('-', 1))
        query.limit(last - first + 1, first)

    field_prefix = json.loads(request.headers.get('x-field-prefix', '""'))
    pref = lambda (f): field_prefix + f

    field_list = json.loads(request.headers.get('x-field-list', "[]"))
    if len(field_list) > 0:
        query.fields(*field_list)

    box = request.headers.get('x-feature-box', None)
    if box:
        query.box()

    like = request.params.get('like', '')
    if like != '':
        query.like(like)

    features = query()

    result = []
    for fobj in features:
        fdata = dict(
            [(pref(k), v) for k, v in fobj.fields.iteritems()],
            id=fobj.id, label=fobj.label)
        if box:
            fdata['box'] = fobj.box.bounds

        result.append(fdata)

    headers = dict()
    headers["Content-Type"] = 'application/json'

    if http_range:
        total = features.total_count
        last = min(total - 1, last)
        headers['Content-Range'] = 'items %d-%s/%d' % (first, last, total)

    return Response(json.dumps(result), headers=headers)


def store_item(layer, request):
    # TODO: Security

    box = request.headers.get('x-feature-box', None)
    ext = request.headers.get('x-feature-ext', None)

    query = layer.feature_query()
    query.filter_by(id=request.matchdict['feature_id'])

    if box:
        query.box()

    feature = list(query())[0]

    result = dict(
        feature.fields,
        id=feature.id, layerId=layer.id,
        fields=feature.fields
    )

    if box:
        result['box'] = feature.box.bounds

    if ext:
        result['ext'] = dict()
        for extcls in FeatureExtension.registry:
            extension = extcls(layer=layer)
            result['ext'][extcls.identity] = extension.feature_data(feature)

    return Response(
        json.dumps(result),
        content_type='application/json')


def setup_pyramid(comp, config):
    DBSession = comp.env.core.DBSession

    class LayerFieldsWidget(ObjectWidget):

        def is_applicable(self):
            return self.operation == 'edit'

        def populate_obj(self):
            obj = self.obj
            data = self.data

            if 'feature_label_field_id' in data:
                obj.feature_label_field_id = data['feature_label_field_id']

            fields = dict(map(lambda fd: (fd['id'], fd), data['fields']))
            for f in obj.fields:
                if f.id in fields:

                    if 'display_name' in fields[f.id]:
                        f.display_name = fields[f.id]['display_name']

                    if 'grid_visibility' in fields[f.id]:
                        f.grid_visibility = fields[f.id]['grid_visibility']

        def widget_module(self):
            return 'feature_layer/LayerFieldsWidget'

        def widget_params(self):
            result = super(LayerFieldsWidget, self).widget_params()

            if self.obj:
                result['value'] = dict(
                    fields=map(lambda f: f.to_dict(), self.obj.fields),
                    feature_label_field_id=self.obj.feature_label_field_id,
                )

            return result

    comp.LayerFieldsWidget = LayerFieldsWidget

    def identify(request):
        """ Сервис идентификации объектов на слоях, поддерживающих интерфейс
        IFeatureLayer """

        srs = int(request.json_body['srs'])
        geom = geom_from_wkt(request.json_body['geom'], srid=srs)
        layers = map(int, request.json_body['layers'])

        layer_list = DBSession.query(Resource).filter(Resource.id.in_(layers))

        result = dict()

        # Количество объектов для всех слоев
        feature_count = 0

        for layer in layer_list:
            if not layer.has_permission(request.user, 'data-read'):
                result[layer.id] = dict(error="Forbidden")

            elif not IFeatureLayer.providedBy(layer):
                result[layer.id] = dict(error="Not implemented")

            else:
                query = layer.feature_query()
                query.intersects(geom)

                # Ограничиваем кол-во идентифицируемых объектов по 10 на слой,
                # иначе ответ может оказаться очень большим.
                query.limit(10)

                features = [
                    dict(id=f.id, layerId=layer.id, label=f.label, fields=f.fields)
                    for f in query()
                ]

                result[layer.id] = dict(
                    features=features,
                    featureCount=len(features)
                )

                feature_count += len(features)

        result["featureCount"] = feature_count

        return result

    config.add_route('feature_layer.identify', '/feature_layer/identify')
    config.add_view(identify, route_name='feature_layer.identify', renderer='json')

    config.add_route(
        'feature_layer.feature.browse',
        '/resource/{id:\d+}/feature/',
        factory=resource_factory,
        client=('id', )
    ).add_view(
        feature_browse, context=IFeatureLayer,
        renderer='nextgisweb:feature_layer/template/feature_browse.mako')

    config.add_route(
        'feature_layer.field', '/resource/{id:\d+}/field/',
        factory=resource_factory,
        client=('id', )
    ).add_view(field_collection, context=IFeatureLayer, renderer='json')

    config.add_route(
        'feature_layer.store',
        '/resource/{id:\d+}/store/',
        factory=resource_factory, client=('id', )
    ).add_view(store_collection, context=IFeatureLayer)

    config.add_route(
        'feature_layer.store.item',
        '/resource/{id:\d+}/store/{feature_id:\d+}',
        factory=resource_factory
    ).add_view(store_item, context=IFeatureLayer)

    config.add_route(
        'feature_layer.feature.show',
        '/resource/{id:\d+}/feature/{feature_id:\d+}',
        factory=resource_factory,
        client=('id', 'feature_id')
    ).add_view(
        feature_show,
        context=IFeatureLayer,
        renderer='feature_layer/feature_show.mako')

    config.add_route(
        'feature_layer.feature.edit',
        '/resource/{id:\d+}/feature/{feature_id}/edit',
        factory=resource_factory,
        client=('id', 'feature_id')
    ).add_view(
        feature_edit,
        context=IFeatureLayer,
        renderer='model_widget.mako')

    def client_settings(self, request):
        return dict(
            extensions=dict(
                map(
                    lambda ext: (ext.identity, dict(
                        displayWidget=ext.display_widget
                    )),
                    FeatureExtension.registry
                )
            ),
            identify=dict(
                attributes=self.settings['identify.attributes']
            ),
        )

    comp.client_settings = MethodType(client_settings, comp, comp.__class__)

    # Расширения меню слоя
    class LayerMenuExt(dm.DynItem):

        def build(self, args):
            if IFeatureLayer.providedBy(args.obj):
                yield dm.Link(
                    'extra/feature-browse', u"Таблица объектов",
                    lambda args: args.request.route_url(
                        "feature_layer.feature.browse",
                        id=args.obj.id
                    )
                )

    Resource.__dynmenu__.add(LayerMenuExt())

    Resource.__psection__.register(
        key='fields', title=u"Атрибуты",
        template="nextgisweb:feature_layer/template/section_fields.mako",
        is_applicable=lambda (obj): IFeatureLayer.providedBy(obj))