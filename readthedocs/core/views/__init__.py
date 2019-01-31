# -*- coding: utf-8 -*-

"""
Core views, including the main homepage,

documentation and header rendering, and server errors.
"""

from __future__ import absolute_import
from __future__ import division
import os
import logging

from django.conf import settings
from django.http import HttpResponseRedirect, Http404, JsonResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.views.generic import TemplateView


from readthedocs.builds.models import Version
from readthedocs.core.resolver import resolve_path
from readthedocs.core.symlink import PrivateSymlink, PublicSymlink
from readthedocs.core.utils import broadcast
from readthedocs.core.views.serve import _serve_file
from readthedocs.projects.constants import PRIVATE
from readthedocs.projects.models import Project, ImportedFile
from readthedocs.projects.tasks import remove_dirs
from readthedocs.redirects.utils import get_redirect_response, project_and_path_from_request

log = logging.getLogger(__name__)


class NoProjectException(Exception):
    pass


class HomepageView(TemplateView):

    template_name = 'homepage.html'

    def get_context_data(self, **kwargs):
        """Add latest builds and featured projects."""
        context = super().get_context_data(**kwargs)
        context['featured_list'] = Project.objects.filter(featured=True)
        context['projects_count'] = Project.objects.count()
        return context


class SupportView(TemplateView):
    template_name = 'support.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        support_email = getattr(settings, 'SUPPORT_EMAIL', None)
        if not support_email:
            support_email = 'support@{domain}'.format(
                domain=getattr(
                    settings,
                    'PRODUCTION_DOMAIN',
                    'readthedocs.org',
                ),
            )

        context['support_email'] = support_email
        return context


def random_page(request, project_slug=None):  # pylint: disable=unused-argument
    imported_file = ImportedFile.objects.order_by('?')
    if project_slug:
        imported_file = imported_file.filter(project__slug=project_slug)
    imported_file = imported_file.first()
    if imported_file is None:
        raise Http404
    url = imported_file.get_absolute_url()
    return HttpResponseRedirect(url)


def wipe_version(request, project_slug, version_slug):
    version = get_object_or_404(
        Version,
        project__slug=project_slug,
        slug=version_slug,
    )
    # We need to check by ``for_admin_user`` here to allow members of the
    # ``Admin`` team (which doesn't own the project) under the corporate site.
    if version.project not in Project.objects.for_admin_user(user=request.user):
        raise Http404('You must own this project to wipe it.')

    if request.method == 'POST':
        del_dirs = [
            os.path.join(version.project.doc_path, 'checkouts', version.slug),
            os.path.join(version.project.doc_path, 'envs', version.slug),
            os.path.join(version.project.doc_path, 'conda', version.slug),
        ]
        for del_dir in del_dirs:
            broadcast(type='build', task=remove_dirs, args=[(del_dir,)])
        return redirect('project_version_list', project_slug)
    return render(
        request,
        'wipe_version.html',
        {'version': version, 'project': version.project},
    )


def server_error_500(request, template_name='500.html'):
    """A simple 500 handler so we get media."""
    r = render(request, template_name)
    r.status_code = 500
    return r


def server_error_404(request, exception=None, template_name='404.html'):  # pylint: disable=unused-argument  # noqa
    """
    A simple 404 handler so we get media.

    .. note::

        Marking exception as optional to make /404/ testing page to work.
    """
    response = get_redirect_response(request, full_path=request.get_full_path())

    # Return a redirect response if there is one
    if response:
        if response.url == request.build_absolute_uri():
            # check that we do have a response and avoid infinite redirect
            log.warning(
                'Infinite Redirect: FROM URL is the same than TO URL. url=%s',
                response.url,
            )
        else:
            return response

    project, path = project_and_path_from_request(request, request.get_full_path())
    version_slug = project.get_default_version()
    version = project.versions.get(slug=version_slug)

    filename = resolve_path(
        project,
        version_slug=version_slug,
        filename='404.html',
        subdomain=True,  # subdomain will make it a "full" path without a URL prefix
    )

    # This breaks path joining, by ignoring the root when given an "absolute" path
    if filename[0] == '/':
        filename = filename[1:]

    if version.privacy_level == PRIVATE:
        symlink = PrivateSymlink(project)
    else:
        symlink = PublicSymlink(project)
    basepath = symlink.project_root
    fullpath = os.path.join(basepath, filename)

    if os.path.exists(fullpath):
        log.info('Serving custom 404.html page for project: %', project.slug)
        r = _serve_file(request, filename, basepath)
    else:
        # Return the default 404 page generated by Read the Docs
        r = render(request, template_name)

    r.status_code = 404
    return r


def do_not_track(request):
    dnt_header = request.META.get('HTTP_DNT')

    # https://w3c.github.io/dnt/drafts/tracking-dnt.html#status-representation
    return JsonResponse(  # pylint: disable=redundant-content-type-for-json-response
        {
            'policy': 'https://docs.readthedocs.io/en/latest/privacy-policy.html',
            'same-party': [
                'readthedocs.org',
                'readthedocs.com',
                'readthedocs.io',           # .org Documentation Sites
                'readthedocs-hosted.com',   # .com Documentation Sites
            ],
            'tracking': 'N' if dnt_header == '1' else 'T',
        }, content_type='application/tracking-status+json',
    )
